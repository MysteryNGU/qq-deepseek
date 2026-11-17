// Reproducible build from pinned Coopanion sources with the Companion adapters.
import {createRequire} from 'node:module';
import {mkdirSync,copyFileSync,readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {spawnSync} from 'node:child_process';
const root=resolve(import.meta.dirname,'../..');
const deps=resolve(root,'data/companion-runtime/coopanion-shell/node_modules');
const require=createRequire(resolve(deps,'package.json'));
const {build}=require('esbuild');
const out=resolve(root,'data/companion-runtime/coopanion-shell/web');mkdirSync(out,{recursive:true});
const petDist=resolve(root,'vendor/coopanion/packages/cortico-world-desktop-pet/web/dist');mkdirSync(petDist,{recursive:true});
copyFileSync(resolve(import.meta.dirname,'dress-draft.js'),resolve(petDist,'companion-dress.js'));
// The standalone upstream dressing page imports this bundle before rendering any figures.
await build({entryPoints:[resolve(root,'vendor/coopanion/packages/cortico-world-desktop-pet/src/webui/dress-dialog.ts')],outdir:resolve(root,'vendor/coopanion/packages/cortico-world-desktop-pet/web/dist'),bundle:true,format:'esm',target:'es2022',platform:'browser',minify:true,nodePaths:[deps]});
await build({entryPoints:[resolve(import.meta.dirname,'chat.ts')],outfile:resolve(out,'chat.js'),bundle:true,format:'esm',minify:true,nodePaths:[deps],alias:{'cortico':resolve(root,'vendor/coopanion/vendor/cortico/src'),'cortico-world-desktop-pet':resolve(root,'vendor/coopanion/packages/cortico-world-desktop-pet')},loader:{'.svg':'dataurl'}});
const result=spawnSync(process.execPath,[resolve(deps,'@tailwindcss/cli/dist/index.mjs'),'-i',resolve(root,'vendor/coopanion/console/styles.css'),'-o',resolve(out,'styles.css'),'--minify'],{cwd:root,stdio:'inherit'});if(result.status)process.exit(result.status);
for(const file of ['index.html','shell.css'])copyFileSync(resolve(import.meta.dirname,file),resolve(out,file));
copyFileSync(resolve(root,'vendor/coopanion/console/features/chat/chat.css'),resolve(out,'chat.css'));
copyFileSync(resolve(root,'vendor/coopanion/console/companion.css'),resolve(out,'companion.css'));
console.log('Built upstream chat and styles:',out);
