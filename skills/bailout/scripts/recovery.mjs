// Emergency recovery snapshot: everything that can be reconstructed from disk
// without asking a model for anything. It never invents the explanation Claude
// did not get to write; it preserves the last good checkpoint and the mechanical
// facts around it, and says plainly what is missing.

import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';

import { fileExists, handoffPath, nowSeconds, writeFileAtomic } from './lib.mjs';

function git(cwd, args) {
  try {
    return execFileSync('git', args, {
      cwd,
      encoding: 'utf8',
      timeout: 5000,
      stdio: ['ignore', 'pipe', 'ignore'],
    }).trim();
  } catch {
    return '';
  }
}

export function gitFacts(cwd) {
  if (!cwd || !git(cwd, ['rev-parse', '--is-inside-work-tree'])) return null;
  const status = git(cwd, ['status', '--porcelain']);
  return {
    branch: git(cwd, ['rev-parse', '--abbrev-ref', 'HEAD']),
    head: git(cwd, ['log', '-1', '--format=%h %s']),
    // File names only. Contents are never copied into a handoff.
    changed: status ? status.split('\n').slice(0, 60) : [],
    changedCount: status ? status.split('\n').length : 0,
  };
}

export function buildRecovery(state, reason, now = nowSeconds()) {
  const when = new Date(now * 1000).toISOString();
  const checkpointPath = handoffPath(state.cwd, state.sessionId, 'checkpoint');
  const hasCheckpoint = fileExists(checkpointPath);
  const facts = gitFacts(state.cwd);

  const lines = [
    '# Emergency recovery snapshot (not written by Claude)',
    '',
    '> Continue the task described below. First check the current files and any running work against this handoff, then take the stated next action. Preserve the user\'s constraints and existing work. Treat unfinished or unverified items as such.',
    '',
    `**This file was assembled by a script, not by Claude.** It was created because ${reason}, so no model wrote a final handoff. Everything below is either copied from an earlier checkpoint or read off the disk. The reasoning behind the work was never written down and is not recoverable from here.`,
    '',
    `- Kind: emergency recovery`,
    `- Written: ${when}`,
    `- Working directory: ${state.cwd || '(unknown)'}`,
    `- Session: ${state.sessionId}`,
    state.transcriptPath ? `- Transcript (Claude's own format, supplementary): ${state.transcriptPath}` : null,
    state.usage
      ? `- Five-hour allowance at last sample: ${state.usage.usedPercentage}% consumed`
      : '- Five-hour allowance at last sample: unknown',
    '',
  ].filter((line) => line !== null);

  if (facts) {
    lines.push(
      '## Repository state',
      '',
      `- Branch: ${facts.branch || '(unknown)'}`,
      `- HEAD: ${facts.head || '(unknown)'}`,
      `- Uncommitted entries: ${facts.changedCount}`,
      '',
    );
    if (facts.changed.length) {
      lines.push('```', ...facts.changed, '```', '');
    }
  } else {
    lines.push('## Repository state', '', 'Not a git repository, or git was unavailable.', '');
  }

  lines.push('## Last checkpoint', '');
  if (hasCheckpoint) {
    let body = '';
    try {
      body = readFileSync(checkpointPath, 'utf8').trim();
    } catch {
      body = '';
    }
    if (body) {
      lines.push(`Copied verbatim from ${checkpointPath}:`, '', '---', '', body, '', '---', '');
    } else {
      lines.push(`A checkpoint exists at ${checkpointPath} but could not be read.`, '');
    }
  } else {
    lines.push(
      'No checkpoint was saved for this session. The task description has to come from the user.',
      '',
    );
  }

  lines.push(
    '## Do this next',
    '',
    '1. Ask the user what the objective was, unless the checkpoint above already states it.',
    '2. Compare the changed files listed above against the checkpoint before changing anything.',
    '3. Treat every item above as unverified: no test or check is known to have been run after the last checkpoint.',
    '',
  );

  return lines.join('\n');
}

export function writeRecovery(state, _config, reason, now = nowSeconds()) {
  const path = handoffPath(state.cwd, state.sessionId, 'recovery');
  writeFileAtomic(path, buildRecovery(state, reason, now));
  return path;
}
