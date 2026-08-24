"use strict";
/**
 * postinstall: create the private virtualenv and install the Python agent.
 *
 * Failure here is never fatal to `npm install` - the wrapper retries lazily on
 * first use and prints the same diagnosis. That way a CI machine without Python
 * does not break an unrelated `npm ci`.
 */

const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const runtime = require("./runtime");

function log(message) {
  process.stdout.write(`rn-agent: ${message}\n`);
}

function warn(message) {
  process.stderr.write(`rn-agent: ${message}\n`);
}

function run(command, args, options = {}) {
  const result = spawnSync(command, args, { stdio: "inherit", ...options });
  return result.status === 0;
}

function install({ quiet = false } = {}) {
  const python = runtime.findPython();
  if (!python) {
    warn(runtime.pythonMissingMessage());
    return false;
  }

  const dir = runtime.runtimeDir();
  const venvPython = runtime.venvPython(dir);
  if (!quiet) log(`using Python ${python.version} (${python.command})`);

  if (!fs.existsSync(venvPython)) {
    fs.mkdirSync(path.dirname(dir), { recursive: true });
    if (!quiet) log(`creating runtime in ${dir}`);
    if (!run(python.command, ["-m", "venv", dir])) {
      warn("could not create the Python virtual environment.");
      warn("On Debian/Ubuntu you may need: sudo apt install python3-venv");
      return false;
    }
  }

  const distribution = runtime.bundledDistribution();
  const target = distribution || "rn-agent";
  if (!quiet) log(`installing ${distribution ? path.basename(distribution) : "rn-agent from PyPI"}`);

  // The bundled wheel pins the agent version to this npm package; its six
  // dependencies still resolve from PyPI, so no --no-index here.
  const pipArgs = ["-m", "pip", "install", "--quiet", "--upgrade", "--disable-pip-version-check"];
  pipArgs.push(target);

  if (!run(venvPython, pipArgs)) {
    warn("could not install the rn-agent Python package.");
    warn("Check your network/proxy, or install the Python package directly:");
    warn("  pipx install rn-agent");
    return false;
  }

  if (!runtime.isInstalled()) {
    warn(`installation finished but ${runtime.cliPath(dir)} is missing.`);
    return false;
  }
  if (!quiet) log("ready - run `rn-agent scan` inside a React Native project");
  return true;
}

if (require.main === module) {
  const ok = install();
  // Never fail the npm install: the wrapper self-heals on first use.
  process.exit(0);
  void ok;
}

module.exports = { install };
