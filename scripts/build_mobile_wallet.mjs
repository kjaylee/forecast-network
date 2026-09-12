#!/usr/bin/env node
/** Reproducible, browser-only SDK bundle and CSP metadata. All output stays in tmp. */
import {createHash} from 'node:crypto';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import {dirname,resolve,join} from 'node:path';
import {readFile,writeFile,mkdir,copyFile} from 'node:fs/promises';
import {spawnSync} from 'node:child_process';

const root=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const temporary=join(root,'tmp');
const dependencies=join(temporary,'mobile-wallet-build');
const output=join(temporary,'mobile-wallet-assets');
const hash=value=>createHash('sha256').update(value).digest('hex');
const cspHash=value=>`'sha256-${createHash('sha256').update(value).digest('base64')}'`;
await mkdir(dependencies,{recursive:true});
await mkdir(output,{recursive:true});
const manifest=await readFile(join(root,'apps/web/package.json'),'utf8');
const lock=await readFile(join(root,'apps/web/package-lock.json'),'utf8');
const installationHash=hash(manifest+lock+'--ignore-scripts --omit=peer');
const marker=join(dependencies,'.installed-lock-sha256');
let installed='';
try{installed=await readFile(marker,'utf8');}catch{}
if(installed!==installationHash){
  await writeFile(join(dependencies,'package.json'),manifest);
  await writeFile(join(dependencies,'package-lock.json'),lock);
  const result=spawnSync('npm',['ci','--ignore-scripts','--omit=peer','--no-fund','--no-audit'],{
    cwd:dependencies,stdio:'inherit',env:{...process.env,TMPDIR:temporary,npm_config_cache:join(temporary,'npm-cache')},
  });
  if(result.error)throw result.error;
  if(result.status!==0)throw new Error('Pinned mobile wallet dependency installation failed');
  await writeFile(marker,installationHash);
}
const require=createRequire(join(dependencies,'package.json'));
const esbuild=require('esbuild');
if(esbuild.version!=='0.28.1')throw new Error('Unexpected mobile wallet bundler version');
const sdkDirectory=join(dependencies,'node_modules/@solana-mobile/wallet-standard-mobile');
const sdkManifest=JSON.parse(await readFile(join(sdkDirectory,'package.json'),'utf8'));
if(sdkManifest.version!=='0.6.0')throw new Error('Unexpected mobile wallet SDK version');
const sdkPath=join(sdkDirectory,'lib/esm/index.browser.js');
const original=await readFile(sdkPath,'utf8');
// The pinned SDK injects Google Fonts. Preserve its UI and protocol, but use its
// declared system font fallbacks instead of allowing external font requests.
const fontInsertion='host.innerHTML = fonts;';
if(original.split(fontInsertion).length!==2)throw new Error('SDK font insertion changed; review the update');
const browserSource=original.replace(fontInsertion,'host.innerHTML = "";');
const styles=new Map([...original.matchAll(/const (css(?:\$\d+)?) = `([\s\S]*?)`;/g)].map(match=>[match[1],match[2]]));
if(styles.size!==7 || [...styles.values()].some(value=>value.includes('${') || value.includes('\\')))throw new Error('SDK style layout changed; review CSP extraction');
const contentStyleNames=[...original.matchAll(/contentStyles = (css(?:\$\d+)?);/g)].map(match=>match[1]);
if(contentStyleNames.length!==5 || !styles.has('css$6') || !styles.has('css$1'))throw new Error('SDK modal style layout changed');
const styleElements=[styles.get('css$1'),...contentStyleNames.map(name=>styles.get('css$6')+styles.get(name))];
const styleAttributes=[...new Set([...original.matchAll(/\bstyle="([^"\n]+)"/g)].map(match=>match[1]))];
const result=await esbuild.build({
  absWorkingDir:root,entryPoints:[join(root,'apps/web/mobile-wallet-entry.mjs')],
  outfile:join(output,'mobile-wallet-sdk.mjs'),bundle:true,format:'esm',platform:'browser',
  conditions:['browser'],target:['es2022'],minify:true,legalComments:'eof',metafile:true,
  nodePaths:[join(dependencies,'node_modules')],
  plugins:[{name:'forecast-local-fonts',setup(build){build.onLoad({filter:/wallet-standard-mobile\/lib\/esm\/index\.browser\.js$/},()=>({contents:browserSource,loader:'js',resolveDir:dirname(sdkPath)}));}}],
});
const inputs=Object.keys(result.metafile.inputs);
if(inputs.some(path=>/react-native|\/lib\/(cjs|esm)\/index\.native\.js|node:/.test(path)))throw new Error('Unexpected native dependency in browser bundle');
const bundle=await readFile(join(output,'mobile-wallet-sdk.mjs'));
const metadata={
  schemaVersion:1,sdkVersion:'0.6.0',bundlerVersion:esbuild.version,
  bundle:'mobile-wallet-sdk.mjs',sha256:hash(bundle),bytes:bundle.length,
  connectSources:['ws://localhost:*/solana-wallet','http://localhost'],
  styleElementHashes:[...new Set(styleElements.map(cspHash))].sort(),
  styleAttributeHashes:styleAttributes.map(cspHash).sort(),
  transformations:['Remove SDK Google Fonts insertion; retain system font fallbacks'],
};
await writeFile(join(output,'csp.json'),JSON.stringify(metadata,null,2)+'\n');
await writeFile(join(output,'metafile.json'),JSON.stringify(result.metafile,null,2)+'\n');
await copyFile(join(sdkDirectory,'LICENSE'),join(output,'mobile-wallet-sdk.LICENSE.txt'));
console.log(JSON.stringify({output,bytes:metadata.bytes,sha256:metadata.sha256,styleHashes:metadata.styleElementHashes.length,attributeHashes:metadata.styleAttributeHashes.length}));
