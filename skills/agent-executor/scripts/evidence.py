"""Host-captured sources and checks. Semantic judgment stays with the conductor.

The host runs capture/review outside a worker's audited invocation. A worker's
report, confidence, or `verified` field cannot mint a review receipt.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def registry(state: dict[str, Any]) -> dict[str, Any]:
    return state.setdefault("evidence", {"sources": {}, "reviews": {}, "resources": {}})


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def age(value: str) -> float:
    stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("evidence timestamp needs a timezone")
    return (dt.datetime.now(dt.timezone.utc) - stamp).total_seconds()


def outside(path: Path, repository: Path) -> None:
    if path == repository or repository in path.parents:
        raise ValueError("evidence artifacts must be outside the audited repository")


def capture(state: dict[str, Any], directory: Path, args: Any, runner: Any, support: Any) -> dict[str, Any]:
    if any(run["status"] not in {"finished", "cancelled"} for run in state["runs"]):
        raise ValueError("finish/recover the active worker before capturing evidence")
    source_id = uuid.uuid4().hex
    folder = directory / "evidence" / source_id
    folder.mkdir(parents=True, mode=0o700)
    repository = Path(state["repository"])
    outside(folder, repository)
    record = {"source_id": source_id, "kind": args.source_kind, "component": args.component,
              "version": args.version, "retrieved_at": now(), "updated_at": args.updated_at,
              "invalidated": None, "authority": args.authority}
    if args.url:
        url = urllib.parse.urlparse(args.url)
        if url.scheme != "https" or not url.netloc or url.username or url.password:
            raise ValueError("documentation capture requires an HTTPS URL without credentials")
        request = urllib.request.Request(args.url, headers={"User-Agent": "agent-executor-evidence/1"})
        with urllib.request.urlopen(request, timeout=30) as response:
            final_url = response.geturl()
            if urllib.parse.urlparse(final_url).scheme != "https":
                raise ValueError("documentation redirected to a non-HTTPS source")
            data = response.read(4 * 1024 * 1024 + 1)
            if len(data) > 4 * 1024 * 1024:
                raise ValueError("source exceeds 4 MiB; capture a relevant versioned page")
            record.update(location=args.url, resolved_location=final_url,
                          last_modified=response.headers.get("Last-Modified"),
                          content_type=response.headers.get("Content-Type"), capture_method="https_fetch")
    elif args.path:
        source_path = args.path.expanduser().resolve(strict=True)
        data = source_path.read_bytes()
        if len(data) > 4 * 1024 * 1024:
            raise ValueError("source exceeds 4 MiB")
        record.update(location=str(source_path), capture_method="local_read", local_sha256=sha(source_path))
    elif args.command:
        with support.workspace_lease(repository, runner.default_agent_cache_dir()) as lease:
            before, identity = runner.git_state(repository, state["task_spec"]["track_paths"]), runner.git_identity(repository)
            result = runner.run_verification_commands([args.command], repo=repository, out_dir=folder,
                timeout_seconds=60, deadline=state["deadline"], lease=lease)
            after, after_identity = runner.git_state(repository, state["task_spec"]["track_paths"]), runner.git_identity(repository)
        record.update(location=args.command, capture_method="host_check", check=result,
                      workspace_fingerprint=after["fingerprint"], git_identity=after_identity)
        if before["fingerprint"] != after["fingerprint"] or identity != after_identity:
            record["invalidated"] = "check_mutated_workspace_or_history"
        if result["status"] != "passed":
            record["invalidated"] = record["invalidated"] or "check_did_not_pass"
        data = json.dumps(result, indent=2).encode()
        for output in result["commands"]:
            for name in ("stdout_path", "stderr_path"):
                path = Path(output[name])
                output[name + "_sha256"] = sha(path)
                data += b"\n" + name.encode() + b"\n" + path.read_bytes()
    else:
        raise ValueError("capture needs a URL, path, or non-mutating command")
    artifact = folder / "source.txt"
    artifact.write_bytes(data)
    artifact.chmod(0o600)
    record.update(artifact=str(artifact), sha256=sha(artifact))
    evidence = registry(state)
    # A changed version/config/source must invalidate dependent knowledge even
    # inside the cache window. Conflicting sources require a fresh review.
    for previous in evidence["sources"].values():
        if previous["component"] == record["component"] and (
            previous["version"] != record["version"] or
            (previous["location"] == record["location"] and previous["capture_method"] == record["capture_method"] and previous["sha256"] != record["sha256"])
        ):
            previous["invalidated"] = "superseded_by:" + source_id
    evidence["sources"][source_id] = record
    return record


def validate_source(source: dict[str, Any], state: dict[str, Any], runner: Any) -> str:
    if source.get("invalidated"):
        raise ValueError("source invalidated: " + source["invalidated"])
    if not 0 <= age(source["retrieved_at"]) <= 86400:
        raise ValueError("source is stale; capture again")
    artifact = Path(source["artifact"])
    if not artifact.is_file() or sha(artifact) != source["sha256"]:
        raise ValueError("captured source content changed")
    if source["capture_method"] == "local_read":
        path = Path(source["location"])
        if not path.is_file() or sha(path) != source["local_sha256"]:
            raise ValueError("local version/config/source changed; capture again")
    if source["capture_method"] == "host_check":
        for command in source["check"]["commands"]:
            for name in ("stdout_path", "stderr_path"):
                if sha(Path(command[name])) != command[name + "_sha256"]:
                    raise ValueError("actual check output changed")
        repository = Path(state["repository"])
        if runner.git_state(repository, state["task_spec"]["track_paths"])["fingerprint"] != source["workspace_fingerprint"] or runner.git_identity(repository) != source["git_identity"]:
            raise ValueError("workspace changed since check; check current work")
    return artifact.read_text(encoding="utf-8", errors="replace")


def capture_checks(state: dict[str, Any], directory: Path, run: dict[str, Any], component: str, version: str) -> dict[str, Any]:
    path = Path(run["result_path"])
    if run["status"] != "finished" or sha(path) != run["result_sha256"]:
        raise ValueError("check capture requires the unchanged terminal runner result")
    result = json.loads(path.read_text())
    checks = result.get("verification", {})
    if result["status"] != "completed" or checks.get("status") != "passed" or not checks.get("commands"):
        raise ValueError("no independently passed runner checks to capture")
    source_id = uuid.uuid4().hex
    folder = directory / "evidence" / source_id
    folder.mkdir(parents=True, mode=0o700)
    content = json.dumps(checks, indent=2).encode()
    for command in checks["commands"]:
        for name in ("stdout_path", "stderr_path"):
            output = Path(command[name])
            command[name + "_sha256"] = sha(output)
            content += b"\n" + name.encode() + b"\n" + output.read_bytes()
    artifact = folder / "source.txt"
    artifact.write_bytes(content)
    artifact.chmod(0o600)
    record = {"source_id": source_id, "kind": "check", "component": component, "version": version,
        "authority": "local", "retrieved_at": result["finished_at"], "updated_at": None, "invalidated": None,
        "location": str(path), "capture_method": "host_check", "check": checks,
        "workspace_fingerprint": result["git_after"]["fingerprint"], "git_identity": result["git_identity_after"],
        "artifact": str(artifact), "sha256": sha(artifact), "run_id": run["run_id"]}
    registry(state)["sources"][source_id] = record
    return record


def verify(state: dict[str, Any], packet: dict[str, Any], runner: Any) -> dict[str, Any]:
    evidence = registry(state)
    for name in ("conclusion", "owner", "question", "invalidation_trigger"):
        if not isinstance(packet.get(name), str) or not packet[name].strip():
            raise ValueError("evidence review requires " + name)
    if not isinstance(packet.get("claims"), list) or not packet["claims"]:
        raise ValueError("evidence review requires source-backed claims")
    versions = packet.get("versions")
    if not isinstance(versions, dict) or not versions:
        raise ValueError("review requires applicable observed versions")
    checked = {}
    for claim in packet["claims"]:
        if claim.get("kind") not in {"fact", "inference", "observation"} or not claim.get("statement"):
            raise ValueError("claim requires a statement and fact/inference/observation kind")
        if not isinstance(claim.get("citations"), list) or not claim["citations"]:
            raise ValueError("every claim must cite captured source content")
        for citation in claim["citations"]:
            source = evidence["sources"].get(citation.get("source_id"))
            if source is None:
                raise ValueError("claim refers to uncaptured source")
            text = validate_source(source, state, runner)
            if claim["kind"] == "fact" and source["authority"] not in {"official", "local"}:
                raise ValueError("a factual claim needs an authoritative source or local observation")
            if versions.get(source["component"]) != source["version"]:
                raise ValueError("source differs from the observed runtime version")
            excerpt = citation.get("excerpt")
            if not isinstance(excerpt, str) or not excerpt.strip() or excerpt not in text:
                raise ValueError("citation excerpt is absent from captured source content")
            if packet.get("disputed_since"):
                disputed = dt.datetime.fromisoformat(packet["disputed_since"].replace("Z", "+00:00"))
                retrieved = dt.datetime.fromisoformat(source["retrieved_at"].replace("Z", "+00:00"))
                if disputed.tzinfo is None:
                    raise ValueError("disputed_since needs a timezone")
                if retrieved < disputed:
                    raise ValueError("unresolved current fact requires capture after the question arose")
            checked[source["source_id"]] = source["sha256"]
    bindings = packet.get("version_sources", {})
    for component, version in versions.items():
        source = evidence["sources"].get(bindings.get(component))
        if source is None or source["kind"] not in {"version", "config"} or source["component"] != component or source["version"] != version:
            raise ValueError("each version requires a captured installed/version/config source")
        if version not in validate_source(source, state, runner):
            raise ValueError("declared version absent from observed version/config content")
        checked[source["source_id"]] = source["sha256"]
    mappings = packet.get("criteria", [])
    expected = state["task_spec"]["acceptance_criteria"]
    if mappings:
        if [mapping.get("criterion") for mapping in mappings] != expected:
            raise ValueError("map every original acceptance criterion in order")
        for mapping in mappings:
            indexes = mapping.get("claim_indexes")
            if not isinstance(indexes, list) or not indexes or any(type(i) is not int or i < 0 or i >= len(packet["claims"]) for i in indexes):
                raise ValueError("each acceptance criterion needs valid evidence claims")
    if not isinstance(packet.get("gaps", []), list):
        raise ValueError("gaps must be a list")
    review_id = uuid.uuid4().hex
    receipt = {**packet, "review_id": review_id, "checked_sources": checked,
               "verified_at": now(), "semantic_owner": packet["owner"],
               "verification": "host_checked_content_and_outputs; conductor_owns_inferences"}
    evidence["reviews"][review_id] = receipt
    return receipt


def receipt(state: dict[str, Any], review_id: str, runner: Any, *, acceptance: bool = False) -> dict[str, Any]:
    evidence = registry(state)
    result = evidence["reviews"].get(review_id)
    if result is None:
        raise ValueError("host evidence review receipt required; worker flags are not verification")
    for source_id, expected in result["checked_sources"].items():
        source = evidence["sources"][source_id]
        validate_source(source, state, runner)
        if source["sha256"] != expected:
            raise ValueError("source changed after review")
    if acceptance and (result.get("gaps") or [mapping["criterion"] for mapping in result.get("criteria", [])] != state["task_spec"]["acceptance_criteria"]):
        raise ValueError("acceptance requires evidence for every criterion and no unresolved gaps")
    return result


def register(state: dict[str, Any], packet: dict[str, Any]) -> dict[str, Any]:
    if packet.get("kind") not in {"skill", "doc_tool", "instruction"} or not packet.get("name") or not packet.get("applicability"):
        raise ValueError("resource requires kind, name, and applicability")
    if packet["kind"] in {"skill", "instruction"}:
        path = Path(packet["path"]).expanduser().resolve(strict=True)
        packet = {**packet, "path": str(path), "sha256": sha(path)}
    elif not packet.get("tool"):
        raise ValueError("doc tool requires the actually discovered callable tool name")
    record = {**packet, "registered_at": now()}
    registry(state)["resources"][packet["name"]] = record
    return record
