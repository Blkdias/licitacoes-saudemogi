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

function namedFunctionSource(name) {
  const token = `function ${name}(`;
  const start = html.indexOf(token);
  if (start < 0) throw new Error(`funcao ${name} nao encontrada`);
  const open = html.indexOf('{', start + token.length);
  if (open < 0) throw new Error(`corpo de ${name} nao encontrado`);
  let depth = 0;
  for (let index = open; index < html.length; index += 1) {
    if (html[index] === '{') depth += 1;
    if (html[index] === '}') {
      depth -= 1;
      if (depth === 0) return html.slice(start, index + 1);
    }
  }
  throw new Error(`corpo de ${name} nao foi fechado`);
}

function compileNamedFunction(name, context = {}) {
  return vm.runInNewContext(`(${namedFunctionSource(name)})`, context, { filename: `regression:${name}` });
}

function regression(name, check) {
  try {
    if (!check()) fail(`regressao funcional: ${name}.`);
  } catch (error) {
    fail(`regressao funcional: ${name}: ${error.message}`);
  }
}

regression('calculo acumulado ignora apenas judiciais, canceladas e outros anos', () => {
  const natureza = '3.3.90.30 - Material de Consumo';
  const context = {
    EstadoDispensas: { dispensas: [
      { id: 1, natureza, subelemento: 'A', status: 'Em Andamento', ordemJudicial: false, data: '2026-01-10', valor: 100 },
      { id: 2, natureza, subelemento: 'A', status: 'Em Andamento', ordemJudicial: true, data: '2026-01-10', valor: 200 },
      { id: 3, natureza, subelemento: 'A', status: 'Cancelada', ordemJudicial: false, data: '2026-01-10', valor: 300 },
      { id: 4, natureza, subelemento: 'A', status: 'Em Andamento', ordemJudicial: false, data: '2025-01-10', valor: 400 }
    ] }
  };
  const totalNatureza = compileNamedFunction('calcularTotalGastoNatureza', context);
  const totalSubelemento = compileNamedFunction('calcularTotalGastoSubelemento', context);
  return totalNatureza(natureza, 2026) === 100 && totalSubelemento(natureza, 'A', 2026) === 100;
});

regression('filtro judicial combina com o tipo de contratacao', () => {
  const estado = { filtros: {}, dispensas: [
    { id: 1, tipo: 'dispensa', ordemJudicial: true, data: '2026-01-01' },
    { id: 2, tipo: 'dispensa', ordemJudicial: true, data: '2026-01-02' },
    { id: 3, tipo: 'dispensa', ordemJudicial: false, data: '2026-01-03' },
    { id: 4, tipo: 'inexigibilidade', data: '2026-01-04' },
    { id: 5, tipo: 'inexigibilidade', ordemJudicial: true, data: '2026-01-05' }
  ] };
  const context = {
    window: { EstadoDispensas: estado, tipoContratacaoV365: item => item.tipo },
    norm365: value => String(value || '').toLowerCase()
  };
  const filtrar = compileNamedFunction('listaFiltrada365', context);
  estado.filtros = { judicial: 'sim' };
  const judiciais = filtrar().length;
  estado.filtros = { judicial: 'nao' };
  const naoJudiciais = filtrar().length;
  estado.filtros = { judicial: 'nao', tipoContratacao: 'inexigibilidade' };
  return judiciais === 3 && naoJudiciais === 2 && filtrar().length === 1;
});

regression('natureza 3.3.50.85 nao recebe limite ficticio nem bloqueia valor', () => {
  const codigo = '3.3.50.85 - Transferencias por meio de Contrato de Gestao';
  const limite = compileNamedFunction('obterLimiteNatureza', { DISPENSA_CONFIG: { limites: { [codigo]: 0 } } });
  const verificar = compileNamedFunction('verificarLimiteParaSubelemento', {
    calcularTotalGastoSubelemento: () => 100,
    obterLimiteNatureza: limite
  });
  return limite(codigo) === 0 && verificar(codigo, '', 500, '2026-01-01').ok === true;
});

regression('geracao encontra vinculos antigos e novos sem duplicar', () => {
  const context = { AppState: { licitacoes: [
    { id: 1, requisicoesOrigem: [10] },
    { id: 2, requisicaoOrigemId: '20' }
  ] } };
  const encontrar = compileNamedFunction('encontrarLicitacaoDaRequisicao', context);
  return encontrar(10)?.id === 1 && encontrar(20)?.id === 2 && encontrar(30) === undefined;
});

regression('geracao preenche o modal e redireciona vinculo existente', () => {
  const option = { value: '10', selected: false };
  const elements = Object.fromEntries([
    'modal-titulo', 'numero-licitacao', 'numero-processo-licitacao', 'objeto', 'setor',
    'responsavel-modal', 'valor'
  ].map(id => [id, { value: '', textContent: '' }]));
  elements['fluxo-licitacao'] = { value: '', options: [{ value: 'padrao' }, { value: '7' }] };
  let aberturas = 0;
  let editada = null;
  const context = {
    AppState: {
      requisicoes: [{ id: 10, numero: '001/2026', numeroProcesso: 'PA-1', objeto: 'Objeto', setorOrigem: 'Setor', responsavel: 'Responsavel', valor: 50, fluxoPersonalizado: true, fluxoId: 7 }],
      licitacoes: []
    },
    SistemaAuth: { verificarPermissao: () => true },
    mostrarNotificacao: () => {},
    adicionarLicitacao: () => { aberturas += 1; },
    editarLicitacao: id => { editada = id; },
    atualizarPreviewFluxoLicitacao: () => {},
    atualizarChipsRequisicoes: () => {},
    document: {
      getElementById: id => elements[id],
      querySelector: selector => selector.includes('10') ? option : null
    },
    setTimeout: callback => callback()
  };
  context.encontrarLicitacaoDaRequisicao = compileNamedFunction('encontrarLicitacaoDaRequisicao', context);
  const gerar = compileNamedFunction('gerarLicitacaoDaRequisicao', context);
  gerar(10);
  const preencheu = aberturas === 1 && option.selected && elements['numero-licitacao'].value === 'LIC-001/2026' &&
    elements['numero-processo-licitacao'].value === 'PA-1' && elements['fluxo-licitacao'].value === '7';
  context.AppState.licitacoes.push({ id: 99, numeroLicitacao: '009/2026', requisicoesOrigem: [10] });
  gerar(10);
  return preencheu && aberturas === 1 && editada === 99;
});

regression('geracao registra origem e preserva fluxo escolhido', () => {
  const context = {
    AppState: { requisicoes: [{ id: 10, numero: '001/2026', fluxoPersonalizado: false }], fluxos: [] },
    FLUXOS_LICITACAO: { padrao: ['Edital'], emergencial: ['Emergencial'] }
  };
  const gerar = compileNamedFunction('criarLicitacaoComFluxoRequisicao', context);
  const licitacao = gerar(10, { responsavel: 'Teste', workflow: ['Fluxo confirmado'] });
  return licitacao.geradaDeRequisicao === true && licitacao.requisicaoOrigemId === 10 &&
    licitacao.workflow[0] === 'Fluxo confirmado' && licitacao.historicoWorkflow.length === 1;
});

const requiredRegressionPatterns = [
  ['funcoes de gasto expostas ao renderer final', /window\.calcularTotalGastoNatureza\s*=\s*calcularTotalGastoNatureza[\s\S]*window\.calcularTotalGastoSubelemento\s*=\s*calcularTotalGastoSubelemento/u],
  ['select judicial lido pelo filtro final', /e\.filtros\.judicial\s*=\s*document\.getElementById\('filtro-judicial-dispensa'\)/u],
  ['ocorrencias oficiais filtradas pela chave JSON', /dados->>%23=not\.is\.null/u],
  ['ocorrencias com datas nulas por ultimo', /order=ocorrido_em\.desc\.nullslast,ordem\.desc/u],
  ['natureza 3.3.50.85 exibida sem limite ficticio', /3\.3\.50\.85 - Transferências por meio de Contrato de Gestão['"]\s*:\s*0/u],
  ['configuracao preserva a natureza sem limite', /semLimiteDispensa\s*\?\s*0\s*:\s*val/u],
  ['renderer nao exibe denominador zero', /limiteAplicavel\s*\?\s*` de <strong>\$\{moeda\(limite\)\}<\/strong>`\s*:\s*''/u],
  ['salvamento aplica metadados da requisicao', /criarLicitacaoComFluxoRequisicao\(requisicoesOrigem\[0\],\s*dadosLicitacao\)/u],
  ['remocao de origem limpa o vinculo bilateral', /delete req\.licitacaoId[\s\S]*Desvinculada da licitação/u],
  ['status terminal da requisicao preservado', /!\['Concluída', 'Cancelada', 'Reprovada'\]\.includes\(req\.status\)/u]
];
for (const [description, pattern] of requiredRegressionPatterns) {
  if (!pattern.test(html)) fail(`protecao ausente: ${description}.`);
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
