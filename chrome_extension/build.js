const esbuild = require("esbuild");

const watch = process.argv.includes("--watch");

const buildOptions = {
  entryPoints: ["src/sidepanel.js"],
  bundle: true,
  outfile: "dist/sidepanel.js",
  format: "iife",
  target: "chrome120",
  minify: !watch,
  sourcemap: watch ? "inline" : false,
  logLevel: "info",
};

if (watch) {
  esbuild.context(buildOptions).then((ctx) => {
    ctx.watch();
    console.log("Watching for changes...");
  });
} else {
  esbuild.build(buildOptions).then(() => {
    console.log("Build complete: dist/sidepanel.js");
  });
}
