import js from "@eslint/js";
import perfectionist from "eslint-plugin-perfectionist";
import unicorn from "eslint-plugin-unicorn";
import globals from "globals";

// House style on top of the recommended sets. Every rule switched off below
// has a one-line reason; everything else from unicorn applies as shipped.
const relaxed = {
  "unicorn/consistent-function-scoping": "off", // small closures stay next to their only caller
  "unicorn/name-replacements": "off", // short conventional names (dir, err, args) are fine
  "unicorn/no-array-callback-reference": "off", // array.map(helper) reads well
  "unicorn/no-array-sort": "off", // sorting a freshly built array needs no copy
  "unicorn/no-await-expression-member": "off", // (await load()).field is clear enough
  "unicorn/no-exports-in-scripts": "off", // CLIs export their logic for the tests
  "unicorn/no-nested-ternary": "off", // replaced by the stricter core rule below
  "unicorn/no-null": "off", // JSON and APIs use null on purpose
  "unicorn/no-process-exit": "off", // command-line tools choose their exit codes
  "unicorn/prefer-simple-condition-first": "off", // guard order follows the logic
  "unicorn/prefer-single-call": "off", // separate pushes can read better
  "unicorn/prefer-ternary": "off", // an if/else is often clearer
  "unicorn/prefer-top-level-await": "off", // entry points catch their own errors
  "unicorn/prevent-abbreviations": "off", // same reason as name-replacements
  "unicorn/single-line-block-comment-style": "off", // one-line /** */ docs are fine
};

export default [
  { ignores: ["node_modules/**", ".yarn/**"] },
  js.configs.recommended,
  unicorn.configs.recommended,
  {
    files: ["**/*.{js,mjs}"],
    languageOptions: { ecmaVersion: "latest", globals: globals.node, sourceType: "module" },
    plugins: { perfectionist },
    rules: {
      ...relaxed,
      "no-nested-ternary": "error",
      "perfectionist/sort-exports": ["error", { type: "natural" }],
      "perfectionist/sort-imports": ["error", { type: "natural" }],
      "perfectionist/sort-named-exports": ["error", { type: "natural" }],
      "perfectionist/sort-named-imports": ["error", { type: "natural" }],
    },
  },
];
