#!/usr/bin/env node
"use strict";
/**
 * `rn-agent` entry point for Node users.
 *
 * Forwards every argument to the Python agent in the private virtualenv,
 * preserving stdio (so Rich keeps its colours) and the exit code (so CI can
 * gate on `rn-agent health`). Installs the runtime on first use if the
 * postinstall step could not.
 */

const { spawnSync } = require("node:child_process");

const runtime = require("./../lib/runtime");

function main() {
  const args = process.argv.slice(2);

  if (!runtime.isInstalled()) {
    const { install } = require("./../lib/install");
    if (!install({ quiet: args.includes("--json") })) {
      process.stderr.write(
        "rn-agent: the Python runtime is not available; see the messages above.\n",
      );
      process.exit(70);
    }
  }

  const result = spawnSync(runtime.cliPath(), args, {
    stdio: "inherit",
    env: {
      ...process.env,
      // Rich detects a terminal through stdout; keep colour when piping to a tty.
      PYTHONUNBUFFERED: "1",
    },
  });

  if (result.error) {
    process.stderr.write(`rn-agent: could not start the agent: ${result.error.message}\n`);
    process.exit(70);
  }
  if (result.signal) {
    process.stderr.write(`rn-agent: terminated by signal ${result.signal}\n`);
    process.exit(130);
  }
  process.exit(result.status === null ? 1 : result.status);
}

main();
