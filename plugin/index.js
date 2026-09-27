// Root entry for plugin loaders that resolve a directory by its index file
// (OpenCode plugin discovery does; package.json "main" is not enough there).
// The implementation lives in src/ and is built into dist/.
export { default } from "./dist/index.js";
