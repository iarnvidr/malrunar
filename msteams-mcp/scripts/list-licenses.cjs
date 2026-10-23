// Lists the production npm packages under <node_modules> with the licence each declares.
//   node list-licenses.cjs <node_modules> <output file>
const fs = require('node:fs');
const path = require('node:path');

const [base, output] = process.argv.slice(2);
const rows = [];
function visit(dir) {
  for (const name of fs.readdirSync(dir)) {
    const folder = path.join(dir, name);
    if (name.startsWith('@')) { visit(folder); continue; }
    const file = path.join(folder, 'package.json');
    if (!fs.existsSync(file)) continue;
    const pkg = JSON.parse(fs.readFileSync(file, 'utf8'));
    const license = typeof pkg.license === 'string' ? pkg.license : JSON.stringify(pkg.license || pkg.licenses || 'see package');
    rows.push(`${pkg.name}@${pkg.version}\t${license}`);
  }
}
visit(base);
fs.writeFileSync(output,
  'Production npm dependencies of msteams-mcp (name@version, licence from package.json); each package keeps its own licence file under app/node_modules/<name>/.\n\n'
  + rows.sort().join('\n') + '\n');
