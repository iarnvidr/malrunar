// Generate a notice of every npm package that esbuild bundled, from esbuild metafiles.
// usage: node licenses.mjs <output.txt> <esbuild-cwd>=<metafile.json>...
// For each bundled node_modules package: name, version, declared license, and the full text of
// its LICENSE/LICENCE/COPYING/NOTICE files. Inputs outside node_modules are Tail's own sources (MIT,
// see TAIL-LICENSE).
import { readFileSync, writeFileSync, readdirSync, existsSync, realpathSync } from 'node:fs';
import { join } from 'node:path';

const [out, ...metafiles] = process.argv.slice(2);
if (!out || metafiles.length === 0) throw new Error('usage: licenses.mjs <output> <esbuild-cwd>=<metafile>...');

const roots = new Map();
for (const spec of metafiles) {
  const [cwd, file] = spec.split('=');
  const meta = JSON.parse(readFileSync(file, 'utf8'));
  for (const input of Object.keys(meta.inputs)) {
    const index = input.lastIndexOf('node_modules/');
    if (index === -1) continue;
    const rest = input.slice(index + 'node_modules/'.length).split('/');
    const name = rest[0].startsWith('@') ? `${rest[0]}/${rest[1]}` : rest[0];
    // Metafile paths are relative to the directory esbuild ran in.
    const root = join(cwd, input.slice(0, index), 'node_modules', name);
    roots.set(realpathSync(root), name);
  }
}

const licenseFile = /^(licen[cs]e|copying|notice|unlicense)(\.|$|-)/i;
const sections = [];
for (const [root, name] of [...roots].sort((a, b) => a[1].localeCompare(b[1]) || a[0].localeCompare(b[0]))) {
  const pkg = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8'));
  const declared = typeof pkg.license === 'string' ? pkg.license
    : pkg.license?.type || (pkg.licenses ? JSON.stringify(pkg.licenses) : 'UNKNOWN');
  const texts = readdirSync(root).filter(f => licenseFile.test(f)).sort()
    .map(f => `--- ${f}\n${readFileSync(join(root, f), 'utf8').trim()}\n`);
  if (declared === 'UNKNOWN' || texts.length === 0) {
    console.error(`warning: ${name}@${pkg.version}: license ${declared}, ${texts.length} license file(s)`);
  }
  sections.push(`================================================================\n${pkg.name}@${pkg.version}\nlicense: ${declared}\n${pkg.repository?.url ? `repository: ${pkg.repository.url}\n` : ''}\n${texts.join('\n') || '(no license file shipped in the package; see the declared license above)\n'}`);
}
writeFileSync(out, `Third-party npm packages bundled into this component (generated at build time from the esbuild metafiles).\n\n${sections.join('\n')}`);
console.log(`licenses.mjs: ${sections.length} packages -> ${out}`);
