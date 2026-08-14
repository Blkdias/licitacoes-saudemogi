const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('index.html', 'utf8');
const failures = [];
const fail = message => failures.push(message);

if (!/^<!doctype html>/i.test(html.trimStart())) fail('index.html deve começar com <!DOCTYPE html>.');

function structuralTags(source) {
  const tags = [];
  let cursor = 0;
  while (cursor < source.length) {
    const start = source.indexOf('<', cursor);
    if (start < 0) break;
    if (source.startsWith('<!--', start)) {
      const end = source.indexOf('-->', start + 4);
      cursor = end < 0 ? source.length : end + 3;
      continue;
    }
    const end = source.indexOf('>', start + 1);
    if (end < 0) break;
    const raw = source.slice(start, end + 1);
    const match = raw.match(/^<\s*(\/?)\s*([a-z][\w:-]*)/i);
    if (!match) {
      cursor = end + 1;
      continue;
    }
    const closing = match[1] === '/';
    const name = match[2].toLowerCase();
    tags.push({ name, closing, start, end: end + 1 });
    if (!closing && (name === 'script' || name === 'style')) {
      const closePattern = new RegExp(`<\\/\\s*${name}\\s*>`, 'ig');
      closePattern.lastIndex = end + 1;
      const close = closePattern.exec(source);
      if (!close) break;
      tags.push({ name, closing: true, start: close.index, end: closePattern.lastIndex });
      cursor = closePattern.lastIndex;
      continue;
    }
    cursor = end + 1;
  }
  return tags;
}

const tags = structuralTags(html);
const bodyClosings = tags.filter(tag => tag.name === 'body' && tag.closing);
const htmlClosings = tags.filter(tag => tag.name === 'html' && tag.closing);
if (bodyClosings.length !== 1) fail(`esperado um </body>; encontrado(s) ${bodyClosings.length}.`);
if (htmlClosings.length !== 1) fail(`esperado um </html>; encontrado(s) ${htmlClosings.length}.`);
if (bodyClosings.length === 1 && htmlClosings.length === 1) {
  const bodyIndex = tags.indexOf(bodyClosings[0]);
  const htmlIndex = tags.indexOf(htmlClosings[0]);
  if (htmlIndex !== bodyIndex + 1 || htmlIndex !== tags.length - 1) {
    fail('nenhuma tag estrutural pode existir depois de </body>, exceto </html>.');
  }
  const residual = html.slice(bodyClosings[0].end, htmlClosings[0].start).replace(/<!--[\s\S]*?-->/g, '').trim();
  if (residual) fail(`texto residual encontrado depois de </body>: ${JSON.stringify(residual.slice(0, 80))}.`);
}

for (const forbidden of ['__AUTH_SUPABASE__', 'service_role', 'sb_secret_']) {
  if (html.includes(forbidden)) fail(`token proibido encontrado: ${forbidden}.`);
}

if (/ConsultaComprasIntegracao\.verDetalhes\s*\(\s*['"]\$\{/u.test(html)) {
  fail('IDs externos não podem ser interpolados em handlers JavaScript inline.');
}
if (/\$\{req\.tipo\}\s+\$\{req\.numero\}[^\n]*req\.objeto/u.test(html)) {
  fail('campos de requisição não podem entrar crus em templates HTML.');
}
if (/onclick=["']filtrarPorSetor\([^"']*\$\{setor/u.test(html)) {
  fail('setor não pode ser interpolado em um handler JavaScript inline.');
}
if (/AuthSeguro\?\.save\?\.\(\{access_token/u.test(html)) {
  fail('callbacks de convite/recuperação não podem persistir tokens diretamente.');
}

const externalScripts = [...html.matchAll(/<script\b[^>]*\bsrc=["']([^"']+)["'][^>]*>/gi)];
for (const match of externalScripts) {
  const url = match[1];
  if (/^https?:\/\//i.test(url) && !/(?:\/|@)\d+\.\d+\.\d+(?:\/|\b)/.test(url)) {
    fail(`script externo sem versão exata: ${url}`);
  }
}

const scriptPattern = /<script\b([^>]*)>([\s\S]*?)<\/script>/gi;
let scriptMatch;
let inlineScripts = 0;
while ((scriptMatch = scriptPattern.exec(html))) {
  if (/\bsrc\s*=/.test(scriptMatch[1])) continue;
  inlineScripts += 1;
  const startLine = html.slice(0, scriptMatch.index).split('\n').length;
  try {
    new vm.Script(scriptMatch[2], { filename: `index.html:inline-${inlineScripts}@${startLine}` });
  } catch (error) {
    fail(`JavaScript inválido no script inline ${inlineScripts}, iniciado na linha ${startLine}: ${error.message}`);
  }
}

if (inlineScripts === 0) fail('nenhum script inline foi encontrado.');

if (failures.length) {
  console.error('Quality check falhou:');
  for (const message of failures) console.error(`- ${message}`);
  process.exit(1);
}

console.log(`Quality check aprovado: ${inlineScripts} scripts inline compilados e ${externalScripts.length} dependências externas versionadas.`);
