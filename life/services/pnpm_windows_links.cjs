// Windows may refuse relative directory symlinks with ENOENT rather than EPERM.
// Restrict fallback to existing directories; preserve all other pnpm failures.
const fs = require('node:fs');
const path = require('node:path');
const { syncBuiltinESMExports } = require('node:module');
const { execFileSync } = require('node:child_process');

function createNativeJunction(target, destination) {
  const quote = value => "'" + value.replaceAll("'", "''") + "'";
  // PowerShell writes the native reparse target; Node can otherwise produce
  // Global\\D: junctions that its own ESM resolver does not canonicalize.
  execFileSync('powershell.exe', ['-NoProfile', '-Command',
    `New-Item -ItemType Junction -Path ${quote(destination)} -Target ${quote(target)} | Out-Null`],
    { windowsHide: true, stdio: 'pipe' });
}

function junctionTarget(error, target, destination, type) {
  if (process.platform !== 'win32' || !['ENOENT', 'EPERM', 'EACCES'].includes(error.code) || type !== 'dir') return null;
  const absolute = path.resolve(path.dirname(destination), target);
  return fs.existsSync(absolute) && fs.existsSync(path.dirname(destination)) ? absolute : null;
}
const originalSync = fs.symlinkSync;
fs.symlinkSync = function (target, destination, type) {
  try { return originalSync(target, destination, type); }
  catch (error) {
    const absolute = junctionTarget(error, target, destination, type);
    if (absolute) return createNativeJunction(absolute, destination);
    throw error;
  }
};
const originalAsync = fs.symlink;
fs.symlink = function (target, destination, type, callback) {
  if (typeof type === 'function') return originalAsync(target, destination, type);
  return originalAsync(target, destination, type, error => {
    const absolute = error && junctionTarget(error, target, destination, type);
    if (absolute) {
      try { createNativeJunction(absolute, destination); callback(null); }
      catch (failure) { callback(failure); }
      return;
    }
    callback(error);
  });
};
const originalPromise = fs.promises.symlink;
fs.promises.symlink = async function (target, destination, type) {
  try { return await originalPromise(target, destination, type); }
  catch (error) {
    const absolute = junctionTarget(error, target, destination, type);
    if (absolute) return createNativeJunction(absolute, destination);
    throw error;
  }
};
syncBuiltinESMExports();
