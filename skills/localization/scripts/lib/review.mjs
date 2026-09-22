import {
  detectRepo,
  fallbackLocales,
  localeFiles,
  localeForFile,
  resolveCatalog,
  sourceFile,
  sourceLocale,
  translationsDir,
} from "./adapters.mjs";
import { effectiveCatalog, loadCatalogBundles } from "./catalog.mjs";

function applyReviewArgument(argument, rest, options) {
  switch (argument) {
    case "--json": {
      options.json = true;
      break;
    }
    case "--path": {
      options.path = rest.shift();
      break;
    }
    case "--catalog": {
      options.catalog = rest.shift();
      break;
    }
    case "--locale": {
      options.locale = rest.shift();
      break;
    }
    // No default
  }
}

// The two advisory catalog checks accept the same intentionally permissive CLI
// shape. Unknown tokens remain ignored for compatibility with their previous
// parsers.
export function parseReviewArgs(argv) {
  const options = { json: false, path: null, catalog: null, locale: null };
  const rest = [...argv];
  while (rest.length > 0) {
    applyReviewArgument(rest.shift(), rest, options);
  }
  return options;
}

// Resolve the source catalog and the requested target bundles once for an
// advisory checker. Candidate rules deliberately stay with their caller.
export function prepareCatalogReview(options) {
  const repo = detectRepo(options.path || process.cwd());
  if (repo.repo === "unknown") return { fatal: `not a known i18n repo: ${repo.reason}` };

  const context = resolveCatalog(repo, options.catalog);
  if (context.error) return { fatal: context.error };

  const dir = translationsDir(context);
  let files;
  try {
    // deepcode ignore: directory comes from the detected repository, not untrusted input; local run with the user's own permissions.
    files = localeFiles(context);
  } catch (error) {
    return { fatal: `cannot read ${dir}: ${error.message}` };
  }

  const sourceFile_ = sourceFile(context);
  if (files.includes(sourceFile_) === false) {
    return { fatal: `source file ${sourceFile_} not found in ${dir}` };
  }

  const sourceLocale_ = sourceLocale(context);
  const { bundles, errors } = loadCatalogBundles(context, dir, files);
  const sourceError = errors.get(sourceLocale_);
  if (sourceError) {
    return { fatal: `cannot read ${sourceFile_}: ${sourceError.error.message}` };
  }

  let targets = files
    .filter((file) => file !== sourceFile_)
    .map((file) => ({ file, locale: localeForFile(context, file) }));
  if (options.locale) {
    targets = targets.filter(
      (target) => target.file === options.locale || target.locale === options.locale,
    );
    if (targets.length === 0) return { fatal: `locale ${options.locale} not found` };
  }

  return {
    context,
    sourceFlat: bundles.get(sourceLocale_).parsed.values,
    targets,
    bundles,
    errors,
  };
}

// Build one result per target locale. Parse and fallback failures are reported
// in the same advisory shape as a successful candidate collection.
export function reviewCatalogLocales(review, candidatesForLocale) {
  const { bundles, context, errors, sourceFlat, targets } = review;
  return targets.map(({ file, locale }) => {
    const parseFailure = errors.get(locale);
    if (parseFailure) {
      return { locale, file, parseError: parseFailure.error.message, candidates: [] };
    }

    const fallbackFailure = fallbackLocales(context, locale).find((candidate) =>
      errors.has(candidate),
    );
    if (fallbackFailure) {
      const failure = errors.get(fallbackFailure);
      return {
        locale,
        file,
        parseError: `fallback file ${failure.file}: ${failure.error.message}`,
        candidates: [],
      };
    }

    const effective = effectiveCatalog(context, locale, bundles);
    return {
      locale,
      file,
      candidates: candidatesForLocale({ context, effective, locale, sourceFlat }),
    };
  });
}
