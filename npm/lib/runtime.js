"use strict";
/**
 * Shared runtime resolution for the npm wrapper.
 *
 * React Native developers live in Node, so `npm install -g rn-agent` is the
 * install they expect. The agent itself is Python, so this wrapper owns a
 * private virtual environment under the user's cache directory and never
 * touches their system Python packages.
 */

const { execFileSync, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const MIN_PYTHON = [3, 11];
const ENV_PYTHON = "RN_AGENT_PYTHON";
const ENV_RUNTIME = "RN_AGENT_RUNTIME";

/** Where the private virtualenv lives (override with RN_AGENT_RUNTIME). */
function runtimeDir() {
  if (process.env[ENV_RUNTIME]) {
    return path.resolve(process.env[ENV_RUNTIME]);
  }
  const base =
    process.platform === "darwin"
      ? path.join(os.homedir(), "Library", "Caches", "rn-agent")
      : process.platform === "win32"
        ? path.join(process.env.LOCALAPPDATA || os.homedir(), "rn-agent")
        : path.join(process.env.XDG_CACHE_HOME || path.join(os.homedir(), ".cache"), "rn-agent");
  return path.join(base, "runtime");
}

function venvBin(dir) {
  return process.platform === "win32" ? path.join(dir, "Scripts") : path.join(dir, "bin");
}

/** Absolute path of the installed console script inside the venv. */
function cliPath(dir = runtimeDir()) {
  const name = process.platform === "win32" ? "rn-agent.exe" : "rn-agent";
  return path.join(venvBin(dir), name);
}

function venvPython(dir = runtimeDir()) {
  const name = process.platform === "win32" ? "python.exe" : "python3";
  return path.join(venvBin(dir), name);
}

function parseVersion(output) {
  const match = /(\d+)\.(\d+)\.(\d+)/.exec(String(output || ""));
  if (!match) return null;
  return [Number(match[1]), Number(match[2]), Number(match[3])];
}

function atLeast(version, minimum) {
  if (!version) return false;
  for (let index = 0; index < minimum.length; index += 1) {
    const left = version[index] || 0;
    const right = minimum[index] || 0;
    if (left > right) return true;
    if (left < right) return false;
  }
  return true;
}

/** Find a Python >= 3.11, honouring RN_AGENT_PYTHON. */
function findPython() {
  const candidates = [];
  if (process.env[ENV_PYTHON]) candidates.push(process.env[ENV_PYTHON]);
  candidates.push("python3.13", "python3.12", "python3.11", "python3", "python");
  for (const candidate of candidates) {
    try {
      const output = execFileSync(candidate, ["--version"], {
        encoding: "utf8",
        stdio: ["ignore", "pipe", "pipe"],
      });
      const version = parseVersion(output);
      if (atLeast(version, MIN_PYTHON)) {
        return { command: candidate, version: version.join(".") };
      }
    } catch {
      // try the next candidate
    }
  }
  return null;
}

function pythonMissingMessage() {
  const hint =
    process.platform === "darwin"
      ? "  brew install python@3.12"
      : process.platform === "win32"
        ? "  winget install Python.Python.3.12"
        : "  sudo apt install python3.12 python3.12-venv";
  return [
    "rn-agent needs Python 3.11 or newer, and none was found on PATH.",
    "",
    "Install it:",
    hint,
    "",
    `Already installed somewhere else? Point rn-agent at it:`,
    `  ${ENV_PYTHON}=/full/path/to/python3.12 npm install -g rn-agent`,
  ].join("\n");
}

/** The bundled wheel/sdist shipped inside the npm package. */
function bundledDistribution() {
  const vendor = path.join(__dirname, "..", "vendor");
  if (!fs.existsSync(vendor)) return null;
  const entries = fs.readdirSync(vendor);
  const wheel = entries.find((name) => name.endsWith(".whl"));
  if (wheel) return path.join(vendor, wheel);
  const sdist = entries.find((name) => name.endsWith(".tar.gz"));
  return sdist ? path.join(vendor, sdist) : null;
}

function isInstalled() {
  return fs.existsSync(cliPath());
}

module.exports = {
  ENV_PYTHON,
  ENV_RUNTIME,
  MIN_PYTHON,
  atLeast,
  bundledDistribution,
  cliPath,
  findPython,
  isInstalled,
  parseVersion,
  pythonMissingMessage,
  runtimeDir,
  spawnSync,
  venvPython,
};
