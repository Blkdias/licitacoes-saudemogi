const fs = require('node:fs');
const crypto = require('node:crypto');
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

regression('ocorrencias usam o detalhe embutido e eliminam duplicatas da tabela', () => {
  const context = {};
  context.extrairOcorrenciasDetalhe = compileNamedFunction('extrairOcorrenciasDetalhe', context);
  context.chaveOcorrenciaOficial = compileNamedFunction('chaveOcorrenciaOficial', context);
  context.tempoOcorrenciaOficial = compileNamedFunction('tempoOcorrenciaOficial', context);
  context.mesclarOcorrenciasOficiais = compileNamedFunction('mesclarOcorrenciasOficiais', context);
  const registro = { dados_detalhe: { tabelas: [
    {
      headers: ['ID', 'Data', 'Descrição'],
      rows: [{ ID: 'item-1', Data: '15/08/2026', 'Descrição': 'Linha de item, não ocorrência' }]
    },
    {
      headers: ['#', 'Data', 'Descrição'],
      rows: [
        { '#': '11629', Data: '01/06/2026', 'Descrição': 'Aguardando documentos' },
        { '#': '11741', Data: '10/06/2026', 'Descrição': 'Conferência de edital' },
        { '#': '12063', Data: '23/06/2026', 'Descrição': 'Análise da PGM' },
        { '#': '12190', Data: '14/07/2026', 'Descrição': 'Retorno da PGM' },
        { '#': '12307', Data: '22/07/2026', 'Descrição': 'Apontamentos respondidos' },
        { '#': '13332', Data: '29/07/2026', 'Descrição': 'Sessão agendada' },
        { '#': '13514', Data: '07/08/2026', 'Descrição': 'Retomada em 11/08' },
        { '#': '13544', Data: '12/08/2026', 'Descrição': 'Retomada em 12/08' },
        { '#': '13590', Data: '14/08/2026', 'Descrição': 'Retomada em 18/08' }
      ]
    }
  ] } };
  const persistida = {
    ordem: 13544,
    ocorrido_em: '2026-08-12T00:00:00Z',
    descricao: 'Retomada em 12/08',
    dados: { '#': '13544', Data: '12/08/2026', 'Descrição': 'Retomada em 12/08' }
  };
  const somenteDetalhe = context.mesclarOcorrenciasOficiais([], registro);
  const mescladas = context.mesclarOcorrenciasOficiais([persistida], registro);
  const dataBr = context.tempoOcorrenciaOficial({ dados: { Data: '14/08/2026' } });
  const dataIso = context.tempoOcorrenciaOficial({ ocorrido_em: '2026-08-12T00:00:00Z' });
  const dataInvalida = context.tempoOcorrenciaOficial({ dados: { Data: 'inválida' } });
  return somenteDetalhe.length === 9 && somenteDetalhe[0].dados['#'] === '13590' &&
    somenteDetalhe.at(-1).dados['#'] === '11629' && mescladas.length === 9 &&
    mescladas.filter(item => item.dados['#'] === '13544').length === 1 &&
    dataBr > dataIso && dataInvalida === 0;
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

regression('dashboard unificado filtra ano e fonte sem deslocar datas ISO', () => {
  const context = { ROTULO_FONTE_V37: { licitacao: 'Licitacoes', requisicao: 'Requisicoes', direta: 'Diretas' } };
  context.normalizarDashboardUnificado = compileNamedFunction('normalizarDashboardUnificado', context);
  context.numeroDashboardUnificado = compileNamedFunction('numeroDashboardUnificado', context);
  context.dataLocalDashboardUnificado = compileNamedFunction('dataLocalDashboardUnificado', context);
  context.classeStatusDashboardUnificado = compileNamedFunction('classeStatusDashboardUnificado', context);
  context.tipoDiretaDashboardUnificado = compileNamedFunction('tipoDiretaDashboardUnificado', context);
  context.chaveRegistroDashboardUnificado = compileNamedFunction('chaveRegistroDashboardUnificado', context);
  context.criarRegistroDashboardUnificado = compileNamedFunction('criarRegistroDashboardUnificado', context);
  context.construirRegistrosDashboardUnificado = compileNamedFunction('construirRegistrosDashboardUnificado', context);
  context.filtrarRegistrosDashboardUnificado = compileNamedFunction('filtrarRegistrosDashboardUnificado', context);
  context.criarSnapshotDashboardUnificado = compileNamedFunction('criarSnapshotDashboardUnificado', context);

  const dados = {
    licitacoes: [
      { id: 1, numeroLicitacao: '1/2025', dataAbertura: '2025-03-01', situacao: 'Em andamento', modalidade: 'Pregao', valor: 'R$ 1.234,56', requisicoesOrigem: [10] },
      { id: 2, numeroLicitacao: '2/2026', dataAbertura: '2026-01-01', situacao: 'Concluida', modalidade: 'Concorrencia', valor: 200, valorFinal: 180 }
    ],
    requisicoes: [
      { id: 10, numero: '10/2025', dataCriacao: '2025-04-02T12:00:00Z', status: 'Em Analise', tipo: 'RC', valor: 50 },
      { id: 11, numero: '11/2026', dataCriacao: '2026-02-01', status: 'Concluida', tipo: 'RS', valor: 70 }
    ],
    diretas: [
      { id: 20, numero: '20/2025', data: '2025-05-01', status: 'Em Andamento', tipoContratacao: 'Dispensa', ordemJudicial: true, valor: 30, consultaCompras: { consulta_tipo: 4, consulta_id: 900 } },
      { id: 21, numero: 'duplicada', data: '2025-05-01', status: 'Em Andamento', tipoContratacao: 'Dispensa', ordemJudicial: true, valor: 30, consultaCompras: { consulta_tipo: 4, consulta_id: 900 } },
      { id: 22, numero: '22/2026', data: '2026-06-01', status: 'Cancelada', tipoContratacao: 'Inexigibilidade', valor: 40 },
      { id: 23, numero: 'data-invalida', data: '42026-04-30', status: '', tipoContratacao: 'Dispensa', valor: 5 }
    ]
  };

  const todos = context.criarSnapshotDashboardUnificado(dados, {});
  const ano2025 = context.criarSnapshotDashboardUnificado(dados, { ano: '2025' });
  const diretasJudiciais = context.criarSnapshotDashboardUnificado(dados, { fonte: 'direta', judicial: 'sim' });
  return context.dataLocalDashboardUnificado('2026-01-01').getFullYear() === 2026 &&
    context.dataLocalDashboardUnificado('42026-04-30') === null &&
    context.classeStatusDashboardUnificado('Suspensa') === 'excecao' &&
    context.numeroDashboardUnificado('R$ 1.234,56') === 1234.56 &&
    todos.registros.length === 7 && ano2025.registros.length === 3 &&
    todos.porStatus.ativo === 3 && todos.porStatus.concluido === 2 &&
    todos.porStatus.excecao === 1 && todos.porStatus.nao_classificado === 1 &&
    diretasJudiciais.registros.length === 1 && todos.qualidade.requisicoesVinculadas === 1 &&
    todos.financeiro.licitacaoEstimado === 1434.56 && todos.financeiro.licitacaoFinal === 180;
});

const brasaoPath = 'assets/branding/mogi-brasao.jpg';
if (!fs.existsSync(brasaoPath)) {
  fail('brasao institucional local nao encontrado.');
} else {
  const brasao = fs.readFileSync(brasaoPath);
  const hash = crypto.createHash('sha256').update(brasao).digest('hex');
  if (brasao.length !== 48038) fail(`tamanho inesperado do brasao: ${brasao.length}.`);
  if (brasao[0] !== 0xff || brasao[1] !== 0xd8 || brasao[2] !== 0xff) fail('assinatura JPEG invalida no brasao.');
  if (hash !== '1e5ba100c7d96a0d4a9adebac792c38eae752a21ed04991c21bee2487ccdde9b') fail(`hash inesperado do brasao: ${hash}.`);
}

const requiredRegressionPatterns = [
  ['funcoes de gasto expostas ao renderer final', /window\.calcularTotalGastoNatureza\s*=\s*calcularTotalGastoNatureza[\s\S]*window\.calcularTotalGastoSubelemento\s*=\s*calcularTotalGastoSubelemento/u],
  ['select judicial lido pelo filtro final', /e\.filtros\.judicial\s*=\s*document\.getElementById\('filtro-judicial-dispensa'\)/u],
  ['consulta publica carregada do snapshot estatico', /new URL\('\.\/data\/consulta-compras\/',document\.baseURI\)/u],
  ['ocorrencias embutidas usadas sem leitura remota', /const andamentos\s*=\s*mesclarOcorrenciasOficiais\(\[\],x\)/u],
  ['leitura interna restrita a sessao autenticada', /podeLerDadosInternos=\['admin','gestor'\]\.includes\(usuario\.nivel\)/u],
  ['natureza 3.3.50.85 exibida sem limite ficticio', /3\.3\.50\.85 - Transferências por meio de Contrato de Gestão['"]\s*:\s*0/u],
  ['configuracao preserva a natureza sem limite', /semLimiteDispensa\s*\?\s*0\s*:\s*val/u],
  ['renderer nao exibe denominador zero', /limiteAplicavel\s*\?\s*` de <strong>\$\{moeda\(limite\)\}<\/strong>`\s*:\s*''/u],
  ['salvamento aplica metadados da requisicao', /criarLicitacaoComFluxoRequisicao\(requisicoesOrigem\[0\],\s*dadosLicitacao\)/u],
  ['remocao de origem limpa o vinculo bilateral', /delete req\.licitacaoId[\s\S]*Desvinculada da licitação/u],
  ['status terminal da requisicao preservado', /!\['Concluída', 'Cancelada', 'Reprovada'\]\.includes\(req\.status\)/u]
  ,['dashboard unificado usa ativo local do brasao', /new URL\('\.\/assets\/branding\/mogi-brasao\.jpg',document\.baseURI\)/u]
  ,['relatorio unificado nao limita tabelas a cinquenta linhas', /body:secao\.linhas/u]
  ,['cabecalho institucional redesenhado fora do hook da tabela', /for\(let pagina=primeiraPaginaSecao;pagina<=ultimaPaginaSecao;pagina\+\+\)\{doc\.setPage\(pagina\);desenharCabecalhoRelatorioV37/u]
  ,['pdfs especificos nao reutilizam graficos do painel unificado', /exportarLicitacoesPDFV37[\s\S]*incluirGraficos:false[\s\S]*exportarContratacoesDiretasPDFV37[\s\S]*incluirGraficos:false/u]
  ,['pdfs preservam conclusao responsavel e subelemento', /cabecalho:\['N\\u00famero','Processo','Objeto','Modalidade','Status','Estimado','Valor final','Abertura','Conclus\\u00e3o','Respons\\u00e1vel'\][\s\S]*cabecalho:\['N\\u00famero','Tipo','Objeto','Natureza','Subelemento'/u]
  ,['filtro de ano usa data de entrada por modulo', /entrada:item\?\.dataAbertura\|\|item\?\.dataCriacao[\s\S]*entrada:item\?\.dataCriacao\|\|item\?\.dataLimite[\s\S]*entrada:item\?\.data\|\|item\?\.dataCriacao/u]
  ,['valores financeiros permanecem separados por modulo', /licitacaoEstimado:[\s\S]*licitacaoFinal:[\s\S]*requisicao:[\s\S]*direta:/u]
  ,['excel detalhado aplica moeda e filtros de coluna', /ws\['!autofilter'\][\s\S]*celula\.z='"R\$" #,##0\.00'/u]
  ,['excel reconcilia excecoes e nao classificados', /Exce\\u00e7\\u00f5es \/ n\\u00e3o classificados[\s\S]*snapshot\.porStatus\.excecao\+snapshot\.porStatus\.nao_classificado/u]
  ,['dash de requisicoes removido da navegacao', /querySelector\('\.tab\[onclick\*="dashboardRequisicoes"\]'\)\?\.remove\(\)/u]
  ,['abas antigas redirecionadas para a visao geral', /\['dashboard','dashboardRequisicoes','unificado'\]\.includes\(abaAtual\)\)abrirDashboardUnificadoV37\(\)/u]
  ,['chamadas legadas sempre abrem a visao geral', /const destino=\['dashboard','dashboardRequisicoes'\]\.includes\(nome\)\?'unificado':nome/u]
  ,['atalho de dashboard abre a visao geral', /Ctrl \+ D = Vis\u00e3o Geral[\s\S]*mostrarAba\('unificado'\)/u]
];
for (const [description, pattern] of requiredRegressionPatterns) {
  if (!pattern.test(html)) fail(`protecao ausente: ${description}.`);
}
if (/<div class="tab" onclick="mostrarAba\('dashboard'\)"><i>[^<]*<\/i> Dashboard<\/div>/u.test(html)) {
  fail('aba Dashboard antiga voltou para a navegacao.');
}
const refreshUnificado = html.match(/if \(aba === 'unificado'\)\s+window\.renderizarDashboardUnificado\?\.\(\);/gu) || [];
if (refreshUnificado.length < 2) fail('a Visao Geral nao acompanha todos os ciclos de atualizacao automatica.');

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
