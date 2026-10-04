/** Apply process and credential restrictions to the pinned SRT Seatbelt profile. */
import childProcess from 'node:child_process';
import fs from 'node:fs';
import { syncBuiltinESMExports } from 'node:module';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const VERSION = '0.0.78';
export const DENIALS = [
  '(deny system-audit)',
  '(deny job-creation)',
  '(deny ipc-posix-shm)',
  '(deny ipc-posix-sem)',
  '(deny mach-lookup (global-name "com.apple.securityd.xpc") (global-name "com.apple.SecurityServer"))',
].join('\n');

/** Parse only the exact macOS command transport emitted by the reviewed SRT. */
export function guardedArguments(command, options, parse, quote, metadataPaths = [], terminalPath = null) {
  if (!Array.isArray(metadataPaths) || metadataPaths.length > 16 || metadataPaths.some(value =>
    typeof value !== 'string' || !path.isAbsolute(value) || value.includes('\0') || /[*?\[\]]/.test(value))) {
    throw new Error('Invalid native metadata probes');
  }
  if (terminalPath !== null && (typeof terminalPath !== 'string' || !/^\/dev\/ttys[0-9]{3,6}$/.test(terminalPath))) {
    throw new Error('Invalid private terminal device');
  }
  if (typeof command !== 'string' || options?.shell !== true) {
    throw new Error('Unrecognized SRT shell transport');
  }
  const arguments_ = parse(command);
  if (!arguments_.every(value => typeof value === 'string') || arguments_[0] !== 'env') {
    throw new Error('Unrecognized SRT environment transport');
  }
  if (quote(arguments_) !== command) throw new Error('Noncanonical SRT shell quoting');
  let index = 1;
  while (arguments_[index] === '-u') {
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(arguments_[index + 1] || '')) {
      throw new Error('Invalid SRT environment removal');
    }
    index += 2;
  }
  while (/^[A-Za-z_][A-Za-z0-9_]*=/.test(arguments_[index] || '')) index += 1;
  if (
    arguments_[index] !== '/usr/bin/sandbox-exec' ||
    arguments_[index + 1] !== '-p' ||
    !/^\(version 1\)\n\(deny default \(with message "[^"\n]+"\)\)\n/.test(arguments_[index + 2] || '') ||
    !path.isAbsolute(arguments_[index + 3] || '') ||
    arguments_[index + 4] !== '-c' ||
    typeof arguments_[index + 5] !== 'string' ||
    arguments_.length !== index + 6
  ) {
    throw new Error('Unrecognized SRT Seatbelt profile transport');
  }
  arguments_[index + 2] += '\n; VaultLens immutable process session and credential boundary\n' + DENIALS + '\n';
  if (metadataPaths.length) {
    // Missing directories have no DIRECTORY vnode. Permit only metadata for
    // their exact names so native optional file lookups can return ENOENT.
    arguments_[index + 2] += '(allow file-read-metadata ' +
      metadataPaths.map(value => '(literal ' + JSON.stringify(value) + ')').join(' ') + ')\n';
  }
  if (terminalPath !== null) {
    arguments_[index + 2] += '; VaultLens supervisor-owned controlling terminal\n' +
      '(allow file-read-data file-write-data file-ioctl (require-all ' +
      '(require-any (literal "/dev/tty") (literal ' + JSON.stringify(terminalPath) + ')) ' +
      '(vnode-type CHARACTER-DEVICE)))\n';
  }
  // env executes the unchanged shell/-c payload from SRT. It must not preload
  // this trusted supervisor hook into a native provider or its MCP servers.
  arguments_.splice(1, 0,
    '-u', 'NODE_OPTIONS',
    '-u', 'VAULTLENS_PROCESS_GUARD_SRT',
    '-u', 'VAULTLENS_PROCESS_GUARD_PYTHON',
    '-u', 'VAULTLENS_PROCESS_GUARD_METADATA',
    '-u', 'VAULTLENS_PROCESS_GUARD_PTY',
    '-u', 'VAULTLENS_PROCESS_GUARD_MARKER');
  return arguments_.slice(1);
}

async function installGuard() {
  if (process.platform !== 'darwin') throw new Error('macOS process guard used on another platform');
  const expected = process.env.VAULTLENS_PROCESS_GUARD_SRT;
  const marker = process.env.VAULTLENS_PROCESS_GUARD_MARKER;
  const python = process.env.VAULTLENS_PROCESS_GUARD_PYTHON;
  const metadataPaths = JSON.parse(process.env.VAULTLENS_PROCESS_GUARD_METADATA || '[]');
  const terminalPath = process.env.VAULTLENS_PROCESS_GUARD_PTY || null;
  if (!expected || !marker || !python || fs.realpathSync(process.argv[1]) !== expected || !path.isAbsolute(python)) {
    throw new Error('SRT process guard requires the exact reviewed CLI');
  }
  if (terminalPath !== null && (!/^\/dev\/ttys[0-9]{3,6}$/.test(terminalPath) ||
    fs.realpathSync(terminalPath) !== terminalPath ||
    !fs.lstatSync(terminalPath).isCharacterDevice() || fs.lstatSync(terminalPath).uid !== process.getuid())) {
    throw new Error('Private terminal must be the supervisor-owned character device');
  }
  const packageRoot = path.dirname(path.dirname(expected));
  const metadata = JSON.parse(fs.readFileSync(path.join(packageRoot, 'package.json'), 'utf8'));
  if (metadata.name !== '@anthropic-ai/sandbox-runtime' || metadata.version !== VERSION || path.basename(expected) !== 'cli.js') {
    throw new Error('SRT process guard version mismatch');
  }
  const { quote } = await import(pathToFileURL(path.join(packageRoot, 'dist/utils/shell-quote.js')));
  const parse = command => {
    if (Buffer.byteLength(command) > 2 * 1024 * 1024) throw new Error('SRT profile exceeds size limit');
    const parsed = childProcess.spawnSync(python,
      ['-I', '-c', 'import json, shlex, sys; print(json.dumps(shlex.split(sys.stdin.read())))'],
      { input: command, encoding: 'utf8', timeout: 2000, maxBuffer: 4 * 1024 * 1024 });
    if (parsed.error || parsed.status !== 0) throw new Error('SRT profile parsing failed');
    return JSON.parse(parsed.stdout);
  };
  const original = childProcess.spawn;
  let applied = false;
  childProcess.spawn = function(command, arguments_, options) {
    const shellOptions = Array.isArray(arguments_) ? options : arguments_;
    if (shellOptions?.shell === true) {
      if (applied) throw new Error('SRT attempted a second sandbox workload');
      const values = guardedArguments(command, shellOptions, parse, quote, metadataPaths, terminalPath);
      const environment = { ...(shellOptions.env || process.env) };
      delete environment.NODE_OPTIONS;
      delete environment.VAULTLENS_PROCESS_GUARD_SRT;
      delete environment.VAULTLENS_PROCESS_GUARD_PYTHON;
      delete environment.VAULTLENS_PROCESS_GUARD_METADATA;
      delete environment.VAULTLENS_PROCESS_GUARD_PTY;
      delete environment.VAULTLENS_PROCESS_GUARD_MARKER;
      const descriptor = fs.openSync(marker,
        fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_NOFOLLOW,
        0o600);
      try {
        fs.writeFileSync(descriptor, JSON.stringify({ version: 1, runtime: VERSION, applied: true }));
      } finally {
        fs.closeSync(descriptor);
      }
      applied = true;
      return original.call(this, '/usr/bin/env', values,
        { ...shellOptions, shell: false, env: environment });
    }
    return original.apply(this, arguments);
  };
  syncBuiltinESMExports();
}

// This module can also be imported by public, isolated parser tests. Runtime
// launch always sets this path and checks the private application marker.
if (process.env.VAULTLENS_PROCESS_GUARD_SRT) await installGuard();
