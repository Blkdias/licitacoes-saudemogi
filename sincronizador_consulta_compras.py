#!/usr/bin/env python3
from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs, urlencode

import requests
from bs4 import BeautifulSoup, Tag
from dotenv import load_dotenv

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
except Exception:
    sync_playwright = None
    class PlaywrightTimeoutError(Exception):
        pass

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / '.env.local')

CONSULTA_URL = os.getenv('CONSULTA_COMPRAS_URL', 'http://consultacompras.pmmc.com.br').rstrip('/')
CONSULTA_LOGIN = os.getenv('CONSULTA_COMPRAS_LOGIN', '').strip()
CONSULTA_SENHA = os.getenv('CONSULTA_COMPRAS_SENHA', '')
SUPABASE_URL = os.getenv('SUPABASE_URL', 'https://tzdpksqrfhpytrwwirlz.supabase.co').rstrip('/')
SUPABASE_KEY = (os.getenv('SUPABASE_SECRET_KEY', '') or os.getenv('SUPABASE_SERVICE_ROLE_KEY', '')).strip().strip('"').strip("'")
ANOS = [x.strip() for x in os.getenv('ANOS', '').split(',') if x.strip()]
UNIDADE = os.getenv('CONSULTA_UNIDADE', '').strip()
MODALIDADES_ENV = [x.strip() for x in os.getenv('CONSULTA_MODALIDADES', '').split(',') if x.strip()]
if any(x.strip().lower() in {'todas','todos','*'} for x in MODALIDADES_ENV):
    MODALIDADES_ENV = []
PAUSA = float(os.getenv('PAUSA_ENTRE_DETALHES', '0.20'))
TIMEOUT = int(os.getenv('TIMEOUT', '60'))
MAX_PAGINAS_SEM_PAGINADOR = int(os.getenv('MAX_PAGINAS_SEM_PAGINADOR', '20'))
DIAG_DIR = BASE_DIR / 'diagnosticos_consulta'
CONSULTA_HEADLESS = os.getenv('CONSULTA_HEADLESS', 'true').strip().lower() not in {'0','false','nao','não','no'}
CONSULTA_BROWSER_EXECUTABLE = os.getenv('CONSULTA_BROWSER_EXECUTABLE', '').strip()

# Identificação institucional dos registros da Secretaria de Saúde e Bem-Estar.
# Pode ser ampliada no .env.local sem alterar o código.
SAUDE_PREFIXOS = [x.strip() for x in os.getenv('CONSULTA_SAUDE_PREFIXOS', '002.011').split(',') if x.strip()]
SAUDE_TERMOS = [x.strip() for x in os.getenv('CONSULTA_SAUDE_TERMOS', 'SECRETARIA DE SAÚDE E BEM-ESTAR').split(';') if x.strip()]

SYNC_VERSION = '36.2'


DETAIL_PATH_RE = re.compile(r'/compras/visualizar/(\d+)/(\d+)(?:$|[/?#"\'\s<])', re.I)
DETAIL_ANY_RE = re.compile(r'(?:https?://[^"\'\s<>]+)?/compras/visualizar/(\d+)/(\d+)', re.I)
DETAIL_JS_RE = re.compile(r'visualizar\s*\(\s*["\']?(\d+)["\']?\s*,\s*["\']?(\d+)["\']?', re.I)


def nowiso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def clean(v):
    return ' '.join(str(v or '').replace('\xa0', ' ').split())


def fold(v):
    return unicodedata.normalize('NFD', clean(v)).encode('ascii', 'ignore').decode().lower()


def key(v):
    return re.sub(r'[^a-z0-9]+', '', fold(v))


def normalize_ref(v):
    s = fold(v).upper()
    m = re.search(r'(\d{1,15})\D+(19\d{2}|20\d{2})', s)
    return f"{int(m.group(1))}/{m.group(2)}" if m else re.sub(r'[^A-Z0-9]', '', s)


def normalize_modalidade(v):
    s = fold(v).upper()
    s = re.sub(r'\bRP\s*[-–—]?\s*', '', s)
    s = re.sub(r'[^A-Z0-9]+', ' ', s).strip()
    aliases = {
        'CONCORRENCIA': 'CONCORRENCIA',
        'CONCORRENCIA ELETRONICA': 'CONCORRENCIA ELETRONICA',
        'PREGAO': 'PREGAO',
        'PREGAO ELETRONICO': 'PREGAO ELETRONICO',
        'DISPENSA': 'DISPENSA',
        'INEXIGIBILIDADE': 'INEXIGIBILIDADE',
    }
    return aliases.get(s, s)


def modalidade_compativel(a, b):
    a = normalize_modalidade(a)
    b = normalize_modalidade(b)
    if not a or not b:
        return False

    def familia(v):
        for pref in ('CONCORRENCIA', 'PREGAO', 'DISPENSA', 'INEXIGIBILIDADE', 'CONVITE', 'TOMADA', 'CONCURSO', 'LEILAO', 'CREDENCIAMENTO'):
            if v.startswith(pref):
                return pref
        return v

    # Considera a família da modalidade: Concorrência e Concorrência Eletrônica
    # podem representar a mesma categoria cadastrada localmente. Pregão jamais
    # é compatível com Concorrência.
    return familia(a) == familia(b)


def walk_text(obj):
    if obj is None:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield clean(k)
            yield from walk_text(v)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            yield from walk_text(v)
    else:
        t = clean(obj)
        if t:
            yield t


def extrair_refs_requisicoes(obj, ano_padrao=None):
    """Extrai referências RC/RS/RP e também linhas genéricas da aba Requisições.

    No Consulta Compras a aba "Requisições" pode trazer somente:
    Requisição | Processo | Data | Unidade Requisitante
    sem informar explicitamente se o número é RC, RS ou RP. Por isso preservamos
    a referência mesmo com tipo vazio; o vínculo posterior só é criado quando
    houver correspondência local única e segura.
    """
    refs = []
    seen = set()

    def add(tipo, numero, ano=None, unidade='', processo='', data=''):
        tipo = fold(tipo).upper()
        if tipo not in {'RC', 'RS', 'RP'}:
            tipo = ''
        raw = clean(numero)
        mnum = re.search(r'0*(\d{1,10})(?:\s*/\s*(20\d{2}))?', raw)
        if not mnum:
            return
        numero_i = str(int(mnum.group(1)))
        ano = str(mnum.group(2) or ano or ano_padrao or '').strip()
        if ano and not re.fullmatch(r'20\d{2}', ano):
            ano = ''
        numero_fmt = f'{numero_i}/{ano}' if ano else numero_i
        unidade = clean(unidade)
        processo = clean(processo)
        data = clean(data)
        base = normalize_ref(numero_fmt)

        # Se chegou uma versão tipada, substitui a referência genérica do mesmo número.
        if tipo:
            refs[:] = [r for r in refs if not (normalize_ref(r.get('numero')) == base and not r.get('tipo'))]
            seen.clear()
            seen.update((r.get('tipo',''), normalize_ref(r.get('numero')), fold(r.get('unidade'))) for r in refs)
        elif any(normalize_ref(r.get('numero')) == base and r.get('tipo') for r in refs):
            return

        chave = (tipo, base, fold(unidade))
        if chave in seen:
            return
        seen.add(chave)
        refs.append({
            'tipo': tipo,
            'numero': numero_fmt,
            'unidade': unidade or None,
            'processo': processo or None,
            'data': data or None,
        })

    def scan_struct(v):
        if isinstance(v, dict):
            items = {key(k): val for k, val in v.items()}
            tipo = clean(items.get('tipo') or items.get('tiporequisicao') or items.get('tiposolicitacao') or '')
            numero = clean(
                items.get('numerorequisicao') or items.get('requisicao') or
                items.get('numerosolicitacao') or items.get('solicitacao') or
                (items.get('numero') if fold(tipo).upper() in {'RC','RS','RP'} else '')
            )
            unidade = clean(items.get('unidaderequisitante') or items.get('unidade') or items.get('requisitante') or '')
            processo = clean(items.get('processo') or items.get('processorequisicao') or '')
            data = clean(items.get('data') or items.get('datarequisicao') or '')
            tipo_fold = fold(tipo).upper()
            if numero and (tipo_fold in {'RC','RS','RP'} or 'requisicao' in items or 'numerorequisicao' in items):
                add(tipo if tipo_fold in {'RC','RS','RP'} else '', numero, unidade=unidade, processo=processo, data=data)
            for x in v.values():
                scan_struct(x)
        elif isinstance(v, list):
            for x in v:
                scan_struct(x)
    scan_struct(obj)

    # Casos textuais: RC 123/2026, RS-00123/2026 etc.
    rx = re.compile(r'\b(RC|RS|RP)\s*(?:N[ºO°.]?\s*)?[-–—:/ ]*0*(\d{1,10})(?:\s*/\s*(20\d{2}))?', re.I)
    for text in walk_text(obj):
        for m in rx.finditer(text):
            add(m.group(1), m.group(2), m.group(3))

    refs.sort(key=lambda r: (normalize_ref(r.get('numero')), 0 if r.get('tipo') else 1, r.get('tipo') or ''))
    return refs


def valor_monetario_campos(fields):
    raw = pick(fields, 'valor estimado', 'valor total', 'valor', 'valor da contratacao', 'valor contratação')
    if not raw:
        return 0.0
    s = re.sub(r'[^0-9,.-]', '', raw)
    if ',' in s:
        s = s.replace('.', '').replace(',', '.')
    try:
        return float(s)
    except Exception:
        return 0.0


def sha(obj):
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def slug(v):
    s = fold(v)
    s = re.sub(r'[^a-z0-9]+', '_', s).strip('_')
    return s[:60] or 'sem_modalidade'


def direct_cells(tr: Tag):
    return [c for c in tr.children if isinstance(c, Tag) and c.name in {'th', 'td'}]


def parse_table(table: Tag):
    headers = []
    thead = table.find('thead')
    if thead and thead.find('tr'):
        headers = [clean(c.get_text(' ', strip=True)) for c in direct_cells(thead.find('tr'))]
    rows = []
    for tr in table.find_all('tr'):
        if thead and thead in tr.parents:
            continue
        cs = direct_cells(tr)
        if not cs:
            continue
        vals = [clean(c.get_text(' ', strip=True)) for c in cs]
        if any(vals):
            rows.append((tr, vals))
    width = max([len(headers), *[len(v) for _, v in rows]], default=0)
    if not headers:
        headers = [f'campo_{i+1}' for i in range(width)]
    headers = (headers + [f'campo_{i+1}' for i in range(len(headers), width)])[:width]
    out = []
    for tr, vals in rows:
        vals = (vals + [''] * width)[:width]
        out.append((tr, dict(zip(headers, vals))))
    return headers, out


def find_detail_ref(node):
    """Encontra id/tipo em href, data-*, onclick ou HTML do botão/linha."""
    if node is None:
        return None
    raw = str(node)
    for rx in (DETAIL_ANY_RE, DETAIL_JS_RE):
        m = rx.search(raw)
        if m:
            return int(m.group(1)), int(m.group(2))
    if isinstance(node, Tag):
        attrs = node.attrs or {}
        id_candidates = ['data-id', 'data-compra', 'data-compra-id', 'data-registro-id', 'data-codigo']
        tipo_candidates = ['data-tipo', 'data-type', 'data-compra-tipo', 'data-registro-tipo']
        cid = next((clean(attrs.get(a)) for a in id_candidates if clean(attrs.get(a)).isdigit()), '')
        ctype = next((clean(attrs.get(a)) for a in tipo_candidates if clean(attrs.get(a)).isdigit()), '')
        if cid and ctype:
            return int(cid), int(ctype)
    return None


def choose_table(soup):
    best = None
    score = (-1, -1, -1)
    for t in soup.find_all('table'):
        raw = str(t)
        refs = len(DETAIL_ANY_RE.findall(raw)) + len(DETAIL_JS_RE.findall(raw))
        rows = len(t.find_all('tr'))
        links = len(t.find_all(['a', 'button']))
        if (refs, rows, links) > score:
            best = t
            score = (refs, rows, links)
    return best


def pairs_from_detail(soup):
    d = {}

    def control_value(el):
        if el is None:
            return ''
        if el.name == 'select':
            opt = el.find('option', selected=True) or el.find('option')
            return clean(opt.get_text(' ', strip=True) if opt else '')
        if el.name == 'textarea':
            return clean(el.get_text(' ', strip=True) or el.get('value', ''))
        if el.name == 'input':
            typ = fold(el.get('type', ''))
            if typ in {'password', 'hidden', 'submit', 'button'}:
                return ''
            return clean(el.get('value', ''))
        return clean(el.get_text(' ', strip=True))

    for dt in soup.find_all('dt'):
        dd = dt.find_next_sibling('dd')
        if dd:
            k = clean(dt.get_text(' ', strip=True)).rstrip(':')
            v = clean(dd.get_text(' ', strip=True))
            if k and v:
                d.setdefault(k, v)

    for tr in soup.find_all('tr'):
        cs = direct_cells(tr)
        if len(cs) == 2:
            k = clean(cs[0].get_text(' ', strip=True)).rstrip(':')
            v = clean(cs[1].get_text(' ', strip=True))
            if k and v and len(k) <= 120:
                d.setdefault(k, v)

    # Formulários do modal: labels apontando para input/textarea/select.
    for lab in soup.find_all('label'):
        k = clean(lab.get_text(' ', strip=True)).rstrip(':')
        if not k or len(k) > 120:
            continue
        ctrl = None
        fid = clean(lab.get('for', ''))
        if fid:
            ctrl = soup.find(id=fid)
        if ctrl is None:
            parent = lab.find_parent(['div','td','fieldset'])
            if parent:
                ctrl = parent.find(['input','textarea','select'])
        if ctrl is None:
            sib = lab.find_next_sibling(['input','textarea','select','div','span'])
            if isinstance(sib, Tag):
                ctrl = sib if sib.name in {'input','textarea','select'} else sib.find(['input','textarea','select'])
        v = control_value(ctrl)
        if v and v != k:
            d.setdefault(k, v)

    # Bootstrap/HTML frequente: strong/b + conteúdo em divs adjacentes.
    for lab in soup.find_all(['strong', 'b']):
        k = clean(lab.get_text(' ', strip=True)).rstrip(':')
        if not k or len(k) > 120:
            continue
        sib = lab.find_next_sibling()
        if sib:
            v = control_value(sib)
            if v and v != k:
                d.setdefault(k, v)
    return d


def pick(fields, *aliases):
    items = [(key(k), v) for k, v in fields.items()]
    aks = [key(a) for a in aliases]
    for a in aks:
        for k, v in items:
            if k == a and clean(v):
                return clean(v)
    for a in aks:
        for k, v in items:
            if a in k and clean(v):
                return clean(v)
    return ''


def parse_date(v):
    v = clean(v)
    if not v:
        return None
    for fmt in ('%d/%m/%Y %H:%M:%S', '%d/%m/%Y %H:%M', '%d/%m/%Y', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            dt = datetime.strptime(v, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            pass
    return None


def candidate_andamentos(tables):
    result = []
    seen_signatures = set()
    order = 0

    # Se foi identificada uma tabela/painel de OCORRÊNCIAS, ela é a fonte
    # preferencial. Evita transformar tabelas cadastrais em "andamento".
    ocorr = [tb for tb in tables if tb.get('eh_ocorrencias')]
    fonte = ocorr or tables

    for tb in fonte:
        headers = tb['headers']
        h = ' '.join(fold(x) for x in headers)
        if not ocorr:
            score = sum(w in h for w in ('tram', 'andamento', 'moviment', 'histor', 'ocorr', 'data', 'setor', 'local', 'respons', 'situac', 'etapa'))
            if score < 2:
                continue
        for row in tb['rows']:
            order += 1
            data = pick(row, 'data ocorrencia', 'data ocorrência', 'data', 'data tramite', 'data trâmite', 'data movimento', 'data andamento', 'dt ocorrencia')
            etapa = pick(row, 'ocorrencia', 'ocorrência', 'etapa', 'situacao', 'situação', 'status', 'fase')
            codigo_ocorrencia = pick(row, '#', 'id ocorrencia', 'id ocorrência', 'codigo ocorrencia', 'código ocorrência')
            if codigo_ocorrencia and not etapa:
                etapa = f'Ocorrência {codigo_ocorrencia}'
            desc = pick(row, 'ocorrencia', 'ocorrência', 'descricao', 'descrição', 'andamento', 'tramite', 'trâmite', 'movimento', 'observacao', 'observação', 'historico', 'histórico', 'complemento')
            loc = pick(row, 'local atual', 'local', 'setor', 'unidade', 'destino', 'origem')
            resp = pick(row, 'responsavel', 'responsável', 'usuario', 'usuário', 'agente', 'servidor')
            if not desc:
                desc = ' | '.join(clean(v) for v in row.values() if clean(v))
            if not any(clean(v) for v in row.values()):
                continue
            signature = sha({'titulo': tb.get('titulo'), 'row': row})
            if signature in seen_signatures:
                order -= 1
                continue
            seen_signatures.add(signature)
            result.append({
                'assinatura': signature,
                'ordem': order,
                'ocorrido_em': parse_date(data),
                'etapa': etapa or None,
                'descricao': desc or None,
                'local_atual': loc or None,
                'responsavel': resp or None,
                'dados': row,
            })
    return result



@dataclass
class PortalResponse:
    text: str
    url: str
    status_code: int = 200
    headers: dict | None = None

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f'{self.status_code} ao acessar {self.url}')


def _browser_executable():
    """Tenta usar navegador já instalado no Windows; retorna None para Chromium do Playwright."""
    if CONSULTA_BROWSER_EXECUTABLE:
        p = Path(os.path.expandvars(CONSULTA_BROWSER_EXECUTABLE))
        if p.exists():
            return str(p)
    candidates = [
        r'%PROGRAMFILES%\BraveSoftware\Brave-Browser\Application\brave.exe',
        r'%PROGRAMFILES(X86)%\BraveSoftware\Brave-Browser\Application\brave.exe',
        r'%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe',
        r'%PROGRAMFILES%\Google\Chrome\Application\chrome.exe',
        r'%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe',
        r'%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe',
        r'%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe',
        r'%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe',
    ]
    for raw in candidates:
        p = Path(os.path.expandvars(raw))
        if p.exists():
            return str(p)
    return None


class Supa:
    def __init__(self, url, key_):
        self.url = url.rstrip('/')
        self.key = (key_ or '').strip().strip('"').strip("'")
        self.kind = self._detect_key_kind(self.key)

    @staticmethod
    def _detect_key_kind(k):
        if k.startswith('sb_secret_'):
            return 'secret'
        if k.startswith('sb_publishable_'):
            return 'publishable'
        if k.count('.') == 2 and k.startswith('eyJ'):
            return 'jwt'
        return 'unknown'

    @property
    def h(self):
        h = {'apikey': self.key, 'Content-Type': 'application/json'}
        if self.kind == 'jwt':
            h['Authorization'] = f'Bearer {self.key}'
        return h

    def diagnosticar_chave(self):
        if not self.key:
            raise RuntimeError('Chave Supabase não informada.')
        if self.kind == 'publishable':
            raise RuntimeError('Foi informada uma chave sb_publishable_. Use uma chave de servidor sb_secret_ ou a service_role legada.')
        if self.kind == 'unknown':
            raise RuntimeError('Formato de chave Supabase não reconhecido.')
        url = f'{self.url}/rest/v1/integracao_execucoes?select=id&limit=1'
        r = requests.get(url, headers=self.h, timeout=TIMEOUT)
        if r.status_code >= 400:
            body = (r.text or '').strip().replace('\n', ' ')[:500]
            raise RuntimeError(f'Falha ao validar a chave Supabase (HTTP {r.status_code}). Resposta: {body}')
        print(f'Supabase OK — chave de servidor reconhecida ({"sb_secret" if self.kind == "secret" else "service_role legada"}).')
        return True

    def get(self, path):
        r = requests.get(f'{self.url}/rest/v1/{path}', headers=self.h, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()

    def post(self, table, rows, on_conflict=None):
        if not rows:
            return []
        q = f'?on_conflict={on_conflict}' if on_conflict else ''
        h = {**self.h, 'Prefer': 'resolution=merge-duplicates,return=representation'}
        r = requests.post(f'{self.url}/rest/v1/{table}{q}', headers=h, json=rows, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json() if r.text else []

    def patch(self, table, query, payload):
        r = requests.patch(
            f'{self.url}/rest/v1/{table}?{query}',
            headers={**self.h, 'Prefer': 'return=representation'},
            json=payload,
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        return r.json() if r.text else []

    def delete(self, table, query):
        r = requests.delete(f'{self.url}/rest/v1/{table}?{query}', headers=self.h, timeout=TIMEOUT)
        r.raise_for_status()


class Consulta:
    """Cliente do Consulta Compras usando um navegador real (Playwright).

    O mapeamento original que funcionou foi feito em navegador. As versões
    anteriores usavam requests diretamente e recebiam a tela de filtros, mas
    sem os registros. Esta versão reproduz a navegação real do portal e mantém
    a mesma sessão para listagem e detalhes.
    """
    def __init__(self, user, pwd):
        self.user = user
        self.pwd = pwd
        self.home_response = None
        self.pw = None
        self.browser = None
        self.context = None
        self.page_obj = None

    def _start(self):
        if self.page_obj is not None:
            return
        if sync_playwright is None:
            raise RuntimeError(
                'Playwright não está instalado. Execute executar_sincronizacao.bat '
                'da v35 ou rode: pip install playwright && python -m playwright install chromium'
            )
        self.pw = sync_playwright().start()
        executable = _browser_executable()
        kwargs = {'headless': CONSULTA_HEADLESS}
        if executable:
            kwargs['executable_path'] = executable
            print(f'Navegador local detectado: {Path(executable).name}')
        else:
            print('Usando Chromium gerenciado pelo Playwright.')
        try:
            self.browser = self.pw.chromium.launch(**kwargs)
        except Exception as exc:
            # Se um executável local falhar, tenta o Chromium gerenciado.
            if executable:
                print(f'Aviso: navegador local falhou ({exc}). Tentando Chromium do Playwright...')
                self.browser = self.pw.chromium.launch(headless=CONSULTA_HEADLESS)
            else:
                raise
        self.context = self.browser.new_context(
            locale='pt-BR',
            ignore_https_errors=True,
            viewport={'width': 1440, 'height': 1000},
        )
        self.page_obj = self.context.new_page()
        self.page_obj.set_default_timeout(TIMEOUT * 1000)

    def _capture(self, nav_response=None):
        status = nav_response.status if nav_response is not None else 200
        headers = nav_response.headers if nav_response is not None else {}
        return PortalResponse(
            text=self.page_obj.content(),
            url=self.page_obj.url,
            status_code=status,
            headers=headers,
        )

    def _goto(self, url, params=None):
        self._start()
        if params:
            q = urlencode(params, doseq=True)
            url = f'{url}?{q}'
        nav = self.page_obj.goto(url, wait_until='domcontentloaded', timeout=TIMEOUT * 1000)
        try:
            self.page_obj.wait_for_load_state('networkidle', timeout=5000)
        except PlaywrightTimeoutError:
            pass
        resp = self._capture(nav)
        resp.raise_for_status()
        if urlparse(resp.url).path.rstrip('/') == '/login':
            raise RuntimeError('Sessão do Consulta Compras foi redirecionada ao login.')
        return resp

    def login(self):
        self._start()
        login_url = urljoin(CONSULTA_URL + '/', 'login')
        nav = self.page_obj.goto(login_url, wait_until='domcontentloaded', timeout=TIMEOUT * 1000)
        if nav is not None and nav.status >= 400:
            raise RuntimeError(f'Falha ao abrir login do Consulta Compras: HTTP {nav.status}')

        login_input = self.page_obj.locator('input[name="login"]')
        senha_input = self.page_obj.locator('input[name="senha"]')
        if login_input.count() == 0 or senha_input.count() == 0:
            raise RuntimeError('Campos login/senha não foram encontrados na página do Consulta Compras.')

        login_input.first.fill(self.user)
        senha_input.first.fill(self.pwd)

        form = senha_input.first.locator('xpath=ancestor::form[1]')
        submit = form.locator('button[type="submit"], input[type="submit"], button:not([type])')
        before = self.page_obj.url
        try:
            if submit.count():
                submit.first.click()
            else:
                senha_input.first.press('Enter')
            self.page_obj.wait_for_load_state('domcontentloaded', timeout=TIMEOUT * 1000)
        except PlaywrightTimeoutError:
            pass

        # Aguarda eventual redirect/JS pós-login.
        for _ in range(20):
            if urlparse(self.page_obj.url).path.rstrip('/') != '/login':
                break
            time.sleep(0.25)

        path = urlparse(self.page_obj.url).path.rstrip('/')
        login_still_visible = self.page_obj.locator('input[name="senha"]').count() > 0
        if path == '/login' or login_still_visible:
            # Captura mensagem pública de erro sem qualquer credencial.
            body = clean(self.page_obj.locator('body').inner_text())[:1200]
            raise RuntimeError(f'Login rejeitado pelo Consulta Compras. Página retornou: {body}')

        if '/compras' not in path:
            nav = self.page_obj.goto(
                urljoin(CONSULTA_URL + '/', 'compras'),
                wait_until='domcontentloaded',
                timeout=TIMEOUT * 1000,
            )
            try:
                self.page_obj.wait_for_load_state('networkidle', timeout=5000)
            except PlaywrightTimeoutError:
                pass
            if nav is not None and nav.status >= 400:
                raise RuntimeError(f'Falha ao abrir /compras após login: HTTP {nav.status}')

        self.home_response = self._capture()
        soup = BeautifulSoup(self.home_response.text, 'html.parser')
        if not soup.find('form', action=lambda x: x and urlparse(urljoin(self.home_response.url, x)).path.rstrip('/') == '/compras'):
            raise RuntimeError('Login concluiu, mas o formulário principal /compras não apareceu.')

        print(
            f'Sessão real de navegador ativa: {self.home_response.url} | '
            f'{len(self.home_response.text)} bytes de HTML'
        )
        return self.home_response

    @staticmethod
    def form_defaults(resp):
        soup = BeautifulSoup(resp.text, 'html.parser')
        forms = soup.find_all('form')
        form = None
        for f in forms:
            action = clean(f.get('action', ''))
            path = urlparse(urljoin(resp.url, action or '')).path.rstrip('/')
            if path == '/compras':
                form = f
                break
        if form is None:
            for f in forms:
                if f.find(attrs={'name': 'lic_modalidade'}):
                    form = f
                    break
        params = {}
        if not form:
            return params
        for inp in form.find_all('input', attrs={'name': True}):
            name = inp.get('name')
            typ = (inp.get('type') or '').lower()
            if typ in {'submit', 'button', 'file', 'password'}:
                continue
            if name == '_token':
                continue
            if typ in {'checkbox', 'radio'} and not inp.has_attr('checked'):
                continue
            params[name] = inp.get('value', '')
        for sel in form.find_all('select', attrs={'name': True}):
            opt = sel.find('option', selected=True) or sel.find('option')
            if opt:
                params[sel.get('name')] = opt.get('value', '')
        for ta in form.find_all('textarea', attrs={'name': True}):
            params[ta.get('name')] = clean(ta.get_text())
        return params

    @staticmethod
    def modalidades(resp):
        soup = BeautifulSoup(resp.text, 'html.parser')
        # Procura somente dentro do formulário /compras.
        form = None
        for f in soup.find_all('form'):
            action = clean(f.get('action', ''))
            if urlparse(urljoin(resp.url, action or '')).path.rstrip('/') == '/compras':
                form = f
                break
        scope = form or soup
        sel = scope.find('select', attrs={'name': 'lic_modalidade'})
        if not sel:
            return []
        result = []
        seen = set()
        for opt in sel.find_all('option'):
            value = clean(opt.get('value', ''))
            text = clean(opt.get_text(' ', strip=True))
            if not value:
                continue
            f = fold(text or value)
            if f in {'selecione', 'selecione...', 'todos', 'todas', 'todas as modalidades', 'todas modalidades'}:
                continue
            if value not in seen:
                seen.add(value)
                result.append((value, text or value))
        return result

    def page(self, ano, page=1, modalidade=None, defaults=None):
        defaults = defaults or {}
        known = [
            'ColunaOrdenacao', 'tipo', 'etapa', 'dentroprazo', 'statusata', 'numeroi', 'ano', 'objeto',
            'unidade', 'processo', 'itemrequisicao', 'itemata', 'placatombamento', 'obs', 'id',
            'dataprimeirotramite_de', 'dataprimeirotramite_ate', 'lic_modalidade', 'lic_situacao',
            'lic_agrupamento', 'lic_nova', 'lic_relpref', 'lic_local', 'lic_responsavel',
        ]
        params = {name: clean(defaults.get(name, '')) for name in known}
        params['ano'] = str(ano)
        if UNIDADE:
            params['unidade'] = UNIDADE
        if modalidade is not None:
            params['lic_modalidade'] = modalidade
        if page and page > 1:
            params['page'] = page
        return self._goto(urljoin(CONSULTA_URL + '/', 'compras'), params)

    def detail(self, cid, ctype):
        """Carrega o detalhe e preserva Dados Gerais, Requisições e Ocorrências.

        O modal do Consulta Compras troca o conteúdo conforme a aba. Capturar apenas
        a última aba fazia perder a tabela de Requisições; capturar apenas a primeira
        fazia perder as Ocorrências. Aqui guardamos snapshots separados e entregamos
        um HTML composto ao parser.
        """
        base = self._goto(urljoin(CONSULTA_URL + '/', f'compras/visualizar/{cid}/{ctype}'))
        page = self.page_obj
        sections = []

        def snapshot(nome):
            try:
                body = page.locator('body').inner_html(timeout=3000)
            except Exception:
                body = page.content()
            sections.append(f'<section data-cc-tab="{nome}" id="cc-tab-{slug(nome)}">{body}</section>')

        def ativar(nome):
            alvo = fold(nome)
            candidatos = page.locator('a,button,[role="tab"],li,[data-toggle="tab"],[data-bs-toggle="tab"]')
            escolhido = None
            for i in range(min(candidatos.count(), 400)):
                el = candidatos.nth(i)
                try:
                    txt = clean(el.inner_text(timeout=250))
                except Exception:
                    continue
                ft = fold(txt)
                if ft == alvo or (alvo in ft and len(ft) <= len(alvo) + 20):
                    escolhido = el
                    break
            if escolhido is None:
                return False
            try:
                escolhido.click(timeout=3000)
            except Exception:
                try:
                    escolhido.evaluate('(e)=>e.click()')
                except Exception:
                    return False
            page.wait_for_timeout(650)
            try:
                page.wait_for_load_state('networkidle', timeout=5000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(250)
            return True

        snapshot('Dados Gerais')

        try:
            if ativar('Requisições'):
                snapshot('Requisições')
            else:
                print('    aviso: aba Requisições não encontrada no detalhe.')
        except Exception as exc:
            print(f'    aviso: não foi possível capturar a aba Requisições ({exc})')

        try:
            if ativar('Ocorrências'):
                snapshot('Ocorrências')
            else:
                print('    aviso: aba Ocorrências não encontrada no detalhe.')
        except Exception as exc:
            print(f'    aviso: não foi possível capturar a aba Ocorrências ({exc})')

        combined = '<html><head><meta charset="utf-8"></head><body>' + ''.join(sections) + '</body></html>'
        return PortalResponse(
            text=combined,
            url=base.url,
            status_code=base.status_code,
            headers=base.headers,
        )

    def close(self):
        try:
            if self.context:
                self.context.close()
        except Exception:
            pass
        try:
            if self.browser:
                self.browser.close()
        except Exception:
            pass
        try:
            if self.pw:
                self.pw.stop()
        except Exception:
            pass
        self.page_obj = None



def discover_pages(soup):
    pages = {1}
    # Paginação por href tradicional.
    for a in soup.select('.pagination a[href], a.page-link[href], a[href*="page="], a[href*="pagina="]'):
        q = parse_qs(urlparse(a.get('href', '')).query)
        for pname in ('page', 'pagina'):
            if q.get(pname) and str(q[pname][0]).isdigit():
                pages.add(int(q[pname][0]))

    # Alguns templates usam data-page/data-pagina ou onclick em vez de href.
    for el in soup.select('.pagination [data-page], .pagination [data-pagina], .pagination button, .pagination a'):
        for attr in ('data-page', 'data-pagina'):
            v = clean(el.get(attr, ''))
            if v.isdigit():
                pages.add(int(v))
        onclick = clean(el.get('onclick', ''))
        for m in re.finditer(r'(?:page|pagina)\s*[=:,(]\s*["\']?(\d+)', onclick, re.I):
            pages.add(int(m.group(1)))
        txt = clean(el.get_text(' ', strip=True))
        if txt.isdigit() and 1 <= int(txt) <= 500:
            pages.add(int(txt))
    return sorted(pages)


def _tipo_consulta_por_linha(fields):
    """Converte o texto da coluna Tipo para o código usado por /compras/visualizar/{id}/{tipo}.

    O mapeamento 4=LICITAÇÃO foi confirmado pelo tráfego real do Consulta Compras:
    ao clicar no botão de olho de uma linha de licitação o portal requisita
    /compras/visualizar/<ID>/4.
    """
    tipo = fold(pick(fields, 'tipo', 'tipo registro', 'tipo do registro'))
    numero = fold(pick(fields, 'numero', 'número'))
    if 'licitacao' in tipo or any(x in numero for x in ('pregao', 'concorrencia', 'convite', 'tomada', 'concurso', 'dispensa', 'inexigibilidade', 'leilao', 'credenciamento')):
        return 4
    return None


def _id_consulta_por_linha(fields):
    raw = pick(fields, 'id', 'codigo', 'código', 'id registro')
    m = re.search(r'\b(\d{1,12})\b', clean(raw))
    return int(m.group(1)) if m else None


def extract_list(resp):
    """Extrai os registros da grade principal.

    Importante: o botão de olho do Consulta Compras não precisa conter o URL de
    detalhe no HTML. Nas telas atuais, a grade já traz o ID do registro em uma
    coluna própria e o JavaScript do botão faz a chamada XHR depois do clique.
    As versões <=35.0 só contavam uma linha quando conseguiam descobrir
    /compras/visualizar/... dentro do HTML, por isso a grade visualmente cheia
    era reportada como 0 registros.
    """
    soup = BeautifulSoup(resp.text, 'html.parser')
    recs = []
    seen = set()

    table = choose_table(soup)
    if table:
        _, rows = parse_table(table)
        for tr, fields in rows:
            # 1) Se o HTML já expõe o endpoint, usa-o.
            ref = find_detail_ref(tr)

            # 2) Fluxo real observado na interface: coluna ID + botão de olho.
            #    Para LICITAÇÃO, o segundo parâmetro do detalhe é 4.
            if not ref:
                cid = _id_consulta_por_linha(fields)
                ctype = _tipo_consulta_por_linha(fields)
                if cid and ctype:
                    ref = (cid, ctype)

            if not ref or ref in seen:
                continue
            seen.add(ref)
            recs.append({
                'consulta_id': ref[0],
                'consulta_tipo': ref[1],
                'url_detalhe': urljoin(CONSULTA_URL + '/', f'compras/visualizar/{ref[0]}/{ref[1]}'),
                'fields': fields,
            })

    # Fallback: botões/links fora de tabela, onclick/data-*.
    if not recs:
        for tag in soup.find_all(['a', 'button', 'input', 'div', 'span']):
            ref = find_detail_ref(tag)
            if not ref or ref in seen:
                continue
            seen.add(ref)
            tr = tag.find_parent('tr')
            if tr:
                cs = direct_cells(tr)
                fields = {f'campo_{i+1}': clean(c.get_text(' ', strip=True)) for i, c in enumerate(cs)}
            else:
                parent = tag.find_parent(['li', 'article', 'div']) or tag
                fields = {'texto': clean(parent.get_text(' ', strip=True))[:4000]}
            recs.append({
                'consulta_id': ref[0],
                'consulta_tipo': ref[1],
                'url_detalhe': urljoin(CONSULTA_URL + '/', f'compras/visualizar/{ref[0]}/{ref[1]}'),
                'fields': fields,
            })

    # Último fallback: procura a rota no HTML cru. Serve para templates JS.
    if not recs:
        for m in DETAIL_ANY_RE.finditer(resp.text):
            ref = (int(m.group(1)), int(m.group(2)))
            if ref in seen:
                continue
            seen.add(ref)
            recs.append({
                'consulta_id': ref[0],
                'consulta_tipo': ref[1],
                'url_detalhe': urljoin(CONSULTA_URL + '/', f'compras/visualizar/{ref[0]}/{ref[1]}'),
                'fields': {},
            })

    return recs, discover_pages(soup)


def extract_detail(resp):
    soup = BeautifulSoup(resp.text, 'html.parser')
    fields = pairs_from_detail(soup)
    tables = []
    for i, t in enumerate(soup.find_all('table'), 1):
        headers, rr = parse_table(t)
        rows = [r for _, r in rr]
        tab_section = t.find_parent('section', attrs={'data-cc-tab': True})
        aba = clean(tab_section.get('data-cc-tab')) if tab_section else ''
        heading = t.find('caption') or t.find_previous(['h1', 'h2', 'h3', 'h4', 'h5', 'legend'])
        titulo = clean(heading.get_text(' ', strip=True)) if isinstance(heading, Tag) else f'Tabela {i}'
        if aba:
            titulo = aba
        ancestor = t.find_parent(['div','section','article'])
        contexto = ''
        if ancestor:
            contexto = ' '.join([
                clean(ancestor.get('id','')),
                clean(' '.join(ancestor.get('class',[]) if isinstance(ancestor.get('class'), list) else [ancestor.get('class','')]))
            ])
            ah = ancestor.find(['h1','h2','h3','h4','h5','legend'])
            if ah:
                contexto += ' ' + clean(ah.get_text(' ', strip=True))
        eh_ocorrencias = 'ocorrenc' in fold((aba or '') + ' ' + titulo + ' ' + contexto)
        if headers or rows:
            tables.append({
                'titulo': titulo, 'aba': aba or None, 'headers': headers, 'rows': rows,
                'eh_ocorrencias': eh_ocorrencias
            })

    # Alguns layouts usam listas/divs em vez de tabela na aba OCORRÊNCIAS.
    for pane in soup.find_all(['div','section','article']):
        ident = clean(pane.get('id','')) + ' ' + clean(' '.join(pane.get('class',[]) if isinstance(pane.get('class'), list) else [pane.get('class','')]))
        tab = clean(pane.get('data-cc-tab',''))
        if 'ocorrenc' not in fold(ident + ' ' + tab):
            continue
        if pane.find('table'):
            continue
        rows=[]
        for el in pane.select('li,.list-group-item,.timeline-item,.media')[:500]:
            txt=clean(el.get_text(' ',strip=True))
            if txt:
                rows.append({'Ocorrência':txt})
        if rows:
            tables.append({'titulo':'Ocorrências','aba':'Ocorrências','headers':['Ocorrência'],'rows':rows,'eh_ocorrencias':True})

    sections = [clean(h.get_text(' ', strip=True)) for h in soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5']) if clean(h.get_text(' ', strip=True))]
    detail = {'campos': fields, 'tabelas': tables, 'secoes': list(dict.fromkeys(sections))}
    detail['requisicoes_oficiais'] = extrair_refs_requisicoes(detail)
    return detail, candidate_andamentos(tables)


def merge_fields(a, b):
    d = dict(a)
    d.update({k: v for k, v in b.items() if clean(v)})
    return d


def normalized(rec, detail):
    f = merge_fields(rec['fields'], detail['campos'])

    numero_bruto = pick(f, 'numero', 'número')
    numero_licitacao = pick(f, 'licitacao', 'numero licitacao', 'n licitacao', 'num licitacao')
    if not numero_licitacao and numero_bruto:
        # Ex.: "PREGÃO ELETRÔNICO - 000098/2026" -> "000098/2026"
        m = re.search(r'(\d{1,12})\s*/\s*(20\d{2})', numero_bruto)
        if m:
            numero_licitacao = f'{m.group(1)}/{m.group(2)}'

    modalidade = pick(f, 'modalidade', 'lic modalidade')
    if not modalidade and numero_bruto and ' - ' in numero_bruto:
        modalidade = clean(numero_bruto.rsplit(' - ', 1)[0])

    return {
        'consulta_id': rec['consulta_id'],
        'consulta_tipo': rec['consulta_tipo'],
        'numero_licitacao': numero_licitacao or None,
        'numero_processo': pick(f, 'processo', 'processo administrativo', 'pa', 'numero processo') or None,
        'objeto': pick(f, 'objeto', 'objeto licitacao', 'descricao objeto') or None,
        'modalidade': modalidade or None,
        'situacao': pick(f, 'situacao', 'status', 'fase licitacao', 'fase', 'etapa') or None,
        'responsavel': pick(f, 'responsavel', 'agente', 'responsavel licitacao') or None,
        'local_atual': pick(f, 'local', 'setor atual', 'local atual', 'unidade atual', 'unidade') or None,
        'primeiro_tramite': parse_date(pick(f, 'primeiro tramite', 'data primeiro tramite')),
        'url_detalhe': rec['url_detalhe'],
        'dados_lista': rec['fields'],
        'dados_detalhe': detail,
        'sincronizado_em': nowiso(),
    }


def sanitize_html(text):
    soup = BeautifulSoup(text, 'html.parser')
    for inp in soup.find_all('input'):
        name = fold(inp.get('name', ''))
        typ = fold(inp.get('type', ''))
        if name in {'_token', 'token', 'senha', 'password'} or typ == 'password':
            if inp.has_attr('value'):
                inp['value'] = '{REMOVIDO}'
    return str(soup)


def save_diagnostic(resp, ano, modalidade, label=''):
    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    soup = BeautifulSoup(resp.text, 'html.parser')
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    base = f'{stamp}_{ano}_{slug(label or modalidade or "sem_modalidade")}'
    raw = resp.text
    summary = {
        'gerado_em': nowiso(),
        'ano': str(ano),
        'modalidade_valor': modalidade,
        'modalidade_rotulo': label,
        'url_final': resp.url,
        'status': resp.status_code,
        'content_type': resp.headers.get('content-type'),
        'tamanho_html': len(raw),
        'titulo': clean(soup.title.get_text(' ', strip=True)) if soup.title else '',
        'quantidade_tabelas': len(soup.find_all('table')),
        'quantidade_linhas_tr': len(soup.find_all('tr')),
        'quantidade_links': len(soup.find_all('a')),
        'ocorrencias_rota_visualizar': len(DETAIL_ANY_RE.findall(raw)),
        'ocorrencias_visualizar_js': len(DETAIL_JS_RE.findall(raw)),
        'texto_pagina': clean(soup.get_text(' ', strip=True))[:6000],
        'tabelas_resumo': [],
        'links_resumo': [],
        'forms': [],
        'selects': [],
    }
    for i, tb in enumerate(soup.find_all('table')[:5], 1):
        headers, rr = parse_table(tb)
        summary['tabelas_resumo'].append({
            'indice': i,
            'headers': headers,
            'linhas': [row for _, row in rr[:10]],
            'texto': clean(tb.get_text(' ', strip=True))[:4000],
        })
    for a in soup.find_all('a', href=True)[:50]:
        href = clean(a.get('href', ''))
        # Não registra javascript ou query strings com tokens.
        if 'token=' in fold(href) or '_token=' in fold(href):
            href = '{REMOVIDO}'
        summary['links_resumo'].append({
            'texto': clean(a.get_text(' ', strip=True))[:300],
            'href': href[:1000],
        })
    for f in soup.find_all('form'):
        summary['forms'].append({
            'method': (f.get('method') or 'GET').upper(),
            'action': f.get('action', ''),
            'campos': sorted({x.get('name') for x in f.find_all(attrs={'name': True}) if x.get('name') and x.get('name') not in {'_token', 'senha', 'password'}}),
        })
    for sel in soup.find_all('select'):
        opts = [{'value': clean(o.get('value', '')), 'texto': clean(o.get_text(' ', strip=True)), 'selected': o.has_attr('selected')} for o in sel.find_all('option')]
        summary['selects'].append({'name': sel.get('name'), 'id': sel.get('id'), 'opcoes': opts[:100]})
    (DIAG_DIR / f'{base}.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    (DIAG_DIR / f'{base}.html').write_text(sanitize_html(raw), encoding='utf-8')
    return DIAG_DIR / f'{base}.json'


def _req_ref_normalizada(req):
    tipo = fold(req.get('tipo')).upper()
    nr = normalize_ref(req.get('numero'))
    return tipo, nr


def _refs_externo(x):
    detail = x.get('dados_detalhe') or {}
    diretas = detail.get('requisicoes_oficiais') if isinstance(detail, dict) else None
    if isinstance(diretas, list) and diretas:
        return diretas
    ano = None
    m = re.search(r'/(20\d{2})', clean(x.get('numero_licitacao')))
    if m:
        ano = m.group(1)
    return extrair_refs_requisicoes(detail, ano)


def match_links(supa, externos):
    """Cria vínculos automáticos conservadores.

    Regras:
    1. Licitação: PA exato e único; ou número + modalidade.
    2. Requisição: referência da aba Requisições (RC/RS/RP quando disponível);
       se o portal omitir o tipo, só aceita número local único.
    3. Se a referência oficial aponta para uma requisição que é origem de uma
       única licitação local de modalidade compatível, usa esse vínculo reverso.
    Nunca usa apenas o número da licitação sem modalidade.
    """
    try:
        rows0 = supa.get('sistema_dados?id=eq.1&select=licitacoes,requisicoes')
        if not rows0:
            return 0
        central = rows0[0]
    except Exception:
        return 0

    lics = central.get('licitacoes') or []
    reqs = central.get('requisicoes') or []
    supa.delete('integracao_vinculos', 'automatico=eq.true')
    rows = []

    def add_link(x, lid=None, rid=None, criterio='', conf=1.0):
        chave_tipo = 'L' if lid is not None else 'R'
        local_id = lid if lid is not None else rid
        chave = f"{chave_tipo}:{x['consulta_id']}:{x['consulta_tipo']}:{local_id}"
        if any(r['chave'] == chave for r in rows):
            return
        rows.append({
            'chave': chave, 'consulta_id': x['consulta_id'], 'consulta_tipo': x['consulta_tipo'],
            'local_licitacao_id': lid, 'local_requisicao_id': rid, 'criterio': criterio,
            'confianca': conf, 'automatico': True, 'atualizado_em': nowiso()
        })

    for x in externos:
        xp = normalize_ref(x.get('numero_processo'))
        xn = normalize_ref(x.get('numero_licitacao'))
        xm = normalize_modalidade(x.get('modalidade'))
        refs = _refs_externo(x)

        # A) Descobre primeiro as requisições locais apontadas pelo detalhe oficial.
        req_matches = []
        for ref in refs:
            rnum = normalize_ref(ref.get('numero'))
            rtipo = fold(ref.get('tipo')).upper()
            if not rnum:
                continue
            candidatos = [r for r in reqs if normalize_ref(r.get('numero')) == rnum]
            if rtipo in {'RC','RS','RP'}:
                candidatos = [r for r in candidatos if fold(r.get('tipo')).upper() == rtipo]
            # Quando o portal não exibe RC/RS/RP, só aceita correspondência única.
            if len(candidatos) == 1:
                r = candidatos[0]
                req_matches.append(r)
                add_link(
                    x, rid=int(r['id']),
                    criterio='requisicao_oficial' if rtipo else 'requisicao_oficial_numero_unico',
                    conf=1.0 if rtipo else .995
                )
            elif len(candidatos) > 1:
                print(f"    requisição ambígua ignorada: {ref.get('tipo') or '?'} {ref.get('numero')} ({len(candidatos)} candidatos locais)")

        # B) PA direto com requisição local, somente se a aba Requisições não resolveu.
        if not req_matches and xp:
            candidatos = [r for r in reqs if normalize_ref(r.get('numeroProcesso')) == xp]
            if len(candidatos) == 1:
                req_matches.append(candidatos[0])
                add_link(x, rid=int(candidatos[0]['id']), criterio='processo_requisicao', conf=1.0)

        lic = None
        crit = None
        conf = None

        # 1) Processo/PA exato — só aceita candidato único.
        if xp:
            candidatos = [l for l in lics if normalize_ref(l.get('numeroProcesso')) == xp]
            if len(candidatos) == 1:
                lic, crit, conf = candidatos[0], 'processo', 1.0

        # 2) Número + modalidade. NUNCA número sozinho.
        if not lic and xn and xm:
            candidatos = [
                l for l in lics
                if normalize_ref(l.get('numeroLicitacao')) == xn
                and modalidade_compativel(l.get('modalidade'), x.get('modalidade'))
            ]
            if len(candidatos) == 1:
                lic, crit, conf = candidatos[0], 'licitacao+modalidade', .995
            elif len(candidatos) > 1:
                print(f"    vínculo ambíguo ignorado: {x.get('modalidade')} {x.get('numero_licitacao')} ({len(candidatos)} candidatos locais)")

        # 3) Vínculo reverso pela requisição de origem.
        # Ajuda quando a numeração/PA do Consulta Compras não coincide com a ficha
        # local, sem recorrer a comparação por objeto.
        if not lic and req_matches and xm:
            req_ids = {str(r.get('id')) for r in req_matches}
            candidatos = []
            for l in lics:
                origens = {str(v) for v in (l.get('requisicoesOrigem') or [])}
                if req_ids & origens and modalidade_compativel(l.get('modalidade'), x.get('modalidade')):
                    candidatos.append(l)
            if len(candidatos) == 1:
                lic, crit, conf = candidatos[0], 'requisicao_origem+modalidade', .99
            elif len(candidatos) > 1:
                print(f"    vínculo por requisição de origem ambíguo ignorado: {x.get('numero_licitacao')}")

        if lic:
            lid = int(lic['id'])
            add_link(x, lid=lid, criterio=crit, conf=conf)
            # A ficha da licitação herda vínculo para as requisições de origem,
            # mas não substitui os vínculos oficiais já identificados acima.
            for rid0 in lic.get('requisicoesOrigem') or []:
                try:
                    add_link(x, rid=int(rid0), criterio='herdado_da_licitacao', conf=conf)
                except Exception:
                    pass

    for row in rows:
        supa.post('integracao_vinculos', [row], 'chave')
    return len(rows)


def _status_dispensa(situacao):
    s = fold(situacao)
    if any(w in s for w in ('conclu', 'homolog', 'finaliz', 'adjudic')):
        return 'Concluída'
    if any(w in s for w in ('cancel', 'revog', 'anulad', 'desert', 'fracass')):
        return 'Cancelada'
    return 'Em Andamento'


def _parse_data_curta(v):
    v = clean(v)
    if not v:
        return None
    for fmt in ('%d/%m/%Y', '%Y-%m-%d', '%d/%m/%Y %H:%M', '%d/%m/%Y %H:%M:%S'):
        try:
            return datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _data_dispensa(x, refs=None):
    """Prioriza a data real exibida na aba Requisições do Consulta Compras."""
    datas = []
    for r in (refs or _refs_externo(x)):
        d = _parse_data_curta(r.get('data'))
        if d:
            datas.append(d)
    if datas:
        return min(datas)

    dt = x.get('primeiro_tramite')
    if dt:
        try:
            return datetime.fromisoformat(dt.replace('Z','+00:00')).date().isoformat()
        except Exception:
            pass
    m = re.search(r'/(20\d{2})', clean(x.get('numero_licitacao')))
    if m:
        return f'{m.group(1)}-01-01'
    return datetime.now().date().isoformat()


def _campos_detalhe_expandidos(x):
    """Une Dados Gerais e linhas estruturadas do detalhe para procurar valor/fornecedor/nota."""
    detail = x.get('dados_detalhe') or {}
    campos = dict(detail.get('campos') or {})
    for tb in detail.get('tabelas') or []:
        if fold(tb.get('aba')) in {'requisicoes', 'ocorrencias'}:
            continue
        for row in tb.get('rows') or []:
            if isinstance(row, dict):
                for k, v in row.items():
                    if clean(k) and clean(v):
                        campos.setdefault(k, v)
    return campos


def _nota_observacao(x):
    campos = _campos_detalhe_expandidos(x)
    return pick(
        campos,
        'nota de observacao', 'nota de observação', 'observacao', 'observação',
        'nota observacao', 'nota', 'justificativa', 'motivo'
    )


def _unidade_sem_codigo(v):
    v = clean(v)
    v = re.sub(r'^\s*\d+(?:\.\d+)+\s*[-–—]\s*', '', v)
    return clean(v)


def _unidades_oficiais(x):
    out = []
    seen = set()
    for r in _refs_externo(x):
        u = clean(r.get('unidade'))
        if not u:
            continue
        k = fold(u)
        if k not in seen:
            seen.add(k)
            out.append(u)
    return out


def _eh_registro_saude(x):
    """Identifica Saúde por unidade/código institucional, sem exigir RC/RS local."""
    refs = _refs_externo(x)
    prefixos = [clean(v) for v in SAUDE_PREFIXOS if clean(v)]
    termos = [fold(v) for v in SAUDE_TERMOS if clean(v)]

    for r in refs:
        unidade = clean(r.get('unidade'))
        fu = fold(unidade)
        if unidade and any(unidade.startswith(p) or p in unidade for p in prefixos):
            return True, 'prefixo_unidade_requisitante', unidade
        if fu and any(t in fu for t in termos):
            return True, 'unidade_requisitante_saude', unidade

    # Fallback conservador: somente termos institucionais explícitos no detalhe.
    detail = x.get('dados_detalhe') or {}
    texto = ' | '.join(walk_text(detail))
    ftexto = fold(texto)
    if any(t in ftexto for t in termos):
        return True, 'texto_institucional_saude', (_unidades_oficiais(x)[0] if _unidades_oficiais(x) else '')
    if any(p and p in texto for p in prefixos):
        return True, 'codigo_institucional_saude', (_unidades_oficiais(x)[0] if _unidades_oficiais(x) else '')
    return False, '', ''


def _inferir_natureza_subelemento(x):
    """Classificação determinística e conservadora; retorna vazio quando não há evidência suficiente."""
    nota = _nota_observacao(x)
    txt = fold(f"{x.get('objeto') or ''} {nota}")

    # Evita classificar equipamento permanente como material de consumo.
    permanente = any(w in txt for w in ('equipamento permanente', 'material permanente', 'mobiliario', 'veiculo'))

    if any(w in txt for w in ('pessoa fisica', 'profissional autonom', 'autonomo')):
        return ('3.3.90.36 - Serviços de Terceiros (PF)', '3.3.90.36.01 - Serviços Técnicos (PF)', .93, 'texto indica pessoa física')

    if any(w in txt for w in ('obra civil', 'reforma', 'ampliacao', 'construcao', 'engenharia', 'adequacao predial')):
        sub = '4.4.90.51.03 - Reformas e Ampliações' if any(w in txt for w in ('reforma','ampliacao')) else '4.4.90.51.02 - Serviços de Engenharia'
        return ('4.4.90.51 - Obras e Serviços de Engenharia', sub, .91, 'objeto indica obra/engenharia')

    if any(w in txt for w in ('software', 'sistema informat', 'tecnologia da informacao', 'licenca de software', 'licenciamento de software')):
        return ('3.3.90.39 - Serviços de Terceiros (PJ)', '3.3.90.39.02 - Serviços de Tecnologia (PJ)', .92, 'objeto indica serviço de tecnologia')
    if any(w in txt for w in ('publicidade', 'divulgacao', 'campanha publicitaria')):
        return ('3.3.90.39 - Serviços de Terceiros (PJ)', '3.3.90.39.03 - Serviços de Publicidade (PJ)', .92, 'objeto indica publicidade')
    if any(w in txt for w in ('prestacao de servico', 'prestação de serviço', 'contratacao de empresa', 'locacao', 'manutencao', 'consultoria', 'servicos de', 'serviço de')):
        return ('3.3.90.39 - Serviços de Terceiros (PJ)', '3.3.90.39.01 - Serviços de Terceiros (PJ)', .88, 'objeto indica prestação de serviços')

    hospitalar = any(w in txt for w in (
        'medicamento', 'farmaco', 'fármaco', 'enfermagem', 'hospitalar', 'curativo',
        'seringa', 'agulha', 'luva', 'insumo de saude', 'insumos de saude', 'loção', 'locao',
        'comprimido', 'capsula', 'ampola', 'frasco', 'mg ', ' mg', 'ml ', ' ml'
    ))
    laboratorio = any(w in txt for w in ('laboratorio', 'reagente', 'coleta laboratorial', 'tubo de coleta'))
    material = any(w in txt for w in ('aquisicao de', 'fornecimento de', 'material', 'insumo', 'medicamento'))

    if not permanente and (hospitalar or laboratorio or material):
        if laboratorio:
            sub = '3.3.90.30.03 - Material de Laboratório'
            motivo = 'objeto indica material de laboratório'
        elif hospitalar:
            sub = '3.3.90.30.04 - Material Hospitalar'
            motivo = 'objeto indica material hospitalar/medicamento'
        else:
            sub = '3.3.90.30.01 - Material de Consumo Geral'
            motivo = 'objeto indica aquisição/fornecimento de material'
        return ('3.3.90.30 - Material de Consumo', sub, .88 if hospitalar or laboratorio else .80, motivo)

    return ('A classificar', '', 0.0, 'sem evidência suficiente no Consulta Compras')


def _tipo_disputa_automatico(mod, nota, base_val=''):
    # Preserva qualquer valor válido já escolhido manualmente.
    if base_val in {'disputa_eletronica', 'escolha_direta'}:
        return base_val
    if mod == 'INEXIGIBILIDADE':
        return 'escolha_direta'
    if 'ordem judicial' in fold(nota):
        return 'escolha_direta'
    return 'disputa_eletronica'


def _setor_automatico(req_matches, unidade_oficial):
    # Prefere setor local específico; evita usar marcadores genéricos como "Ordem Judicial".
    for r in req_matches:
        setor = clean(r.get('setorOrigem') or r.get('setorAtual') or '')
        if setor and fold(setor) not in {'ordem judicial', 'orgao solicitante', 'órgão solicitante'}:
            return setor
    if unidade_oficial:
        return _unidade_sem_codigo(unidade_oficial)
    return ''


def _refs_enriquecidas(refs, reqs):
    """Preserva todas as requisições oficiais e, quando possível, anexa a ficha local correspondente."""
    out = []
    matches = []
    vistos_local = set()
    for ref in refs:
        item = dict(ref)
        rn = normalize_ref(ref.get('numero'))
        rtipo = fold(ref.get('tipo')).upper()
        candidatos = [r for r in reqs if normalize_ref(r.get('numero')) == rn] if rn else []
        if rtipo in {'RC','RS','RP'}:
            candidatos = [r for r in candidatos if fold(r.get('tipo')).upper() == rtipo]
        if len(candidatos) == 1:
            r = candidatos[0]
            item['local_id'] = r.get('id')
            item['tipo_local'] = r.get('tipo')
            item['objeto_local'] = r.get('objeto')
            item['setor_local'] = r.get('setorOrigem') or r.get('setorAtual')
            # Se o Consulta omitiu RC/RS/RP, exibe o tipo real somente quando a correspondência é única.
            if not item.get('tipo') and fold(r.get('tipo')).upper() in {'RC','RS','RP'}:
                item['tipo'] = fold(r.get('tipo')).upper()
            if str(r.get('id')) not in vistos_local:
                matches.append(r)
                vistos_local.add(str(r.get('id')))
        out.append(item)
    return out, matches


def sync_auto_dispensas(supa, externos):
    """Cria/atualiza TODA Dispensa/Inexigibilidade identificada como da Saúde.

    A partir da v36.2, a existência de uma RC/RS local não é requisito. A âncora
    principal é a Unidade requisitante oficial do Consulta Compras (ex.: código
    002.011 / SECRETARIA DE SAÚDE E BEM-ESTAR). Quando a requisição também existe
    no sistema local, os dados locais enriquecem a ficha, mas não controlam a inclusão.
    """
    try:
        rows0 = supa.get('sistema_dados?id=eq.1&select=dispensas,requisicoes')
        if not rows0:
            return (0, 0, 0)
        central = rows0[0]
    except Exception as exc:
        print(f'Aviso: não foi possível carregar dispensas para atualização automática: {exc}')
        return (0, 0, 0)

    dispensas = list(central.get('dispensas') or [])
    reqs = central.get('requisicoes') or []
    novos = atualizados = ignorados_fora_saude = 0

    for x in externos:
        mod = normalize_modalidade(x.get('modalidade'))
        if mod not in {'DISPENSA', 'INEXIGIBILIDADE'}:
            continue

        eh_saude, criterio_saude, unidade_saude = _eh_registro_saude(x)
        if not eh_saude:
            ignorados_fora_saude += 1
            continue

        refs_oficiais = _refs_externo(x)
        req_refs, req_matches = _refs_enriquecidas(refs_oficiais, reqs)

        consulta_key = f"{x['consulta_id']}:{x['consulta_tipo']}"
        existente = next((d for d in dispensas if clean((d.get('consultaCompras') or {}).get('chave')) == consulta_key), None)

        campos = _campos_detalhe_expandidos(x)
        valor_oficial = valor_monetario_campos(campos)
        fornecedor_oficial = pick(
            campos, 'fornecedor', 'nome fornecedor', 'fornecedor contratado', 'contratada',
            'adjudicataria', 'adjudicatária', 'empresa', 'favorecido'
        )
        nota = _nota_observacao(x)
        ordem_judicial_oficial = 'ordem judicial' in fold(nota)
        unidade_oficial = unidade_saude or (next((r.get('unidade') for r in req_refs if clean(r.get('unidade'))), '') if req_refs else '')
        natureza_inf, sub_inf, conf_inf, motivo_inf = _inferir_natureza_subelemento(x)

        base = existente or {}
        natureza_base = clean(base.get('natureza'))
        sub_base = clean(base.get('subelemento'))
        pode_atualizar_natureza = (not natureza_base or fold(natureza_base) in {'a classificar', 'selecione', 'selecione...'})
        natureza_final = natureza_inf if pode_atualizar_natureza and natureza_inf else (natureza_base or 'A classificar')
        sub_final = sub_inf if (not sub_base and natureza_final == natureza_inf) else sub_base

        setor_auto = _setor_automatico(req_matches, unidade_oficial)
        refs_texto = ', '.join(f"{(r.get('tipo') or 'Req.')} {r.get('numero')}" for r in req_refs if clean(r.get('numero')))
        obs_auto = 'Sincronizada automaticamente do Consulta Compras.'
        if unidade_oficial:
            obs_auto += f' Unidade requisitante: {unidade_oficial}.'
        if refs_texto:
            obs_auto += f' Requisições oficiais: {refs_texto}.'

        cc = {
            'chave': consulta_key, 'consulta_id': x['consulta_id'], 'consulta_tipo': x['consulta_tipo'],
            'modalidade': x.get('modalidade'), 'numero_licitacao': x.get('numero_licitacao'),
            'numero_processo': x.get('numero_processo'), 'situacao': x.get('situacao'),
            'local_atual': x.get('local_atual'), 'responsavel': x.get('responsavel'),
            'requisicoes': req_refs, 'url_detalhe': x.get('url_detalhe'), 'sincronizado_em': x.get('sincronizado_em'),
            'origem_saude': True, 'criterio_saude': criterio_saude, 'unidade_saude': unidade_oficial or None,
            'nota_observacao': nota or None,
            'natureza_inferida': natureza_inf if natureza_inf != 'A classificar' else None,
            'subelemento_inferido': sub_inf or None,
            'confianca_natureza': conf_inf or None,
            'motivo_natureza': motivo_inf or None,
        }

        registro = dict(base)
        registro.update({
            'id': base.get('id') or (800_000_000_000 + int(x['consulta_id'])),
            'numero': x.get('numero_licitacao') or base.get('numero') or f"CC-{x['consulta_id']}",
            'objeto': x.get('objeto') or base.get('objeto') or 'Objeto não informado',
            'natureza': natureza_final,
            'subelemento': sub_final,
            'valor': (base.get('valor') if float(base.get('valor') or 0) > 0 else (valor_oficial or 0)),
            'data': base.get('data') if base.get('data') and not (base.get('autoConsultaCompras') and str(base.get('data')).endswith('-01-01')) else _data_dispensa(x, req_refs),
            'fornecedor': base.get('fornecedor') or fornecedor_oficial or '',
            'justificativa': base.get('justificativa') or nota or '',
            'status': _status_dispensa(x.get('situacao')),
            'observacoes': base.get('observacoes') or obs_auto,
            'setor': base.get('setor') if base.get('setor') and fold(base.get('setor')) not in {'ordem judicial', 'orgao solicitante'} else setor_auto,
            'tipoDisputa': _tipo_disputa_automatico(mod, nota, base.get('tipoDisputa') or ''),
            'ordemJudicial': bool(base.get('ordemJudicial', False) or ordem_judicial_oficial),
            'autoConsultaCompras': True,
            'naturezaInferidaConsultaCompras': bool(natureza_final == natureza_inf and natureza_inf != 'A classificar' and not natureza_base),
            'consultaCompras': cc,
            'dataCriacao': base.get('dataCriacao') or nowiso(),
            'dataAtualizacao': nowiso(),
        })

        if existente:
            idx = dispensas.index(existente)
            dispensas[idx] = registro
            atualizados += 1
        else:
            dispensas.append(registro)
            novos += 1

    if novos or atualizados:
        supa.patch('sistema_dados', 'id=eq.1', {
            'dispensas': dispensas, 'atualizado_em': nowiso(), 'atualizado_por': 'Sincronizador Consulta Compras v36.2'
        })
    return novos, atualizados, ignorados_fora_saude



def main():
    global CONSULTA_LOGIN, CONSULTA_SENHA, SUPABASE_KEY, ANOS
    if not CONSULTA_LOGIN:
        CONSULTA_LOGIN = input('Login Consulta Compras: ').strip()
    if not CONSULTA_SENHA:
        CONSULTA_SENHA = getpass.getpass('Senha Consulta Compras: ')
    if not SUPABASE_KEY:
        SUPABASE_KEY = getpass.getpass('Supabase Secret key (sb_secret_...) ou service_role legada: ').strip().strip('"').strip("'")
    if not ANOS:
        y = datetime.now().year
        ANOS = [str(y - 1), str(y)]

    supa = Supa(SUPABASE_URL, SUPABASE_KEY)
    consulta = Consulta(CONSULTA_LOGIN, CONSULTA_SENHA)

    print('Validando acesso ao Supabase...')
    supa.diagnosticar_chave()
    execrow = supa.post('integracao_execucoes', [{'status': 'executando', 'anos': ','.join(ANOS)}])
    execid = execrow[0]['id'] if execrow else None
    total = details = andcount = 0
    externos = []

    try:
        print('Autenticando no Consulta Compras...')
        home = consulta.login()
        print('OK')

        defaults = consulta.form_defaults(home)
        extras_proibidos = sorted(set(defaults) & {'data_de', 'data_ate', 'previsao'})
        if extras_proibidos:
            # Não deveria ocorrer na v34.3; apenas proteção adicional.
            for k in extras_proibidos:
                defaults.pop(k, None)
        print('Formulário principal /compras identificado corretamente.')
        print('Filtros estranhos removidos: data_de, data_ate, previsao.')
        if MODALIDADES_ENV:
            modalidades = [(m, m) for m in MODALIDADES_ENV]
            print(f'Modalidades definidas no .env.local: {len(modalidades)}')
            print('ATENÇÃO: filtro CONSULTA_MODALIDADES está ativo; modalidades fora desta lista NÃO serão sincronizadas.')
        else:
            modalidades = consulta.modalidades(home)
            if modalidades:
                print(f'Modalidades encontradas automaticamente no Consulta Compras: {len(modalidades)}')
                for _, rotulo in modalidades[:20]:
                    print(f'  - {rotulo}')
                if len(modalidades) > 20:
                    print(f'  ... e mais {len(modalidades) - 20}')
            else:
                modalidades = None
                print('O formulário inicial ainda não exibiu modalidades; a v34.3 tentará descobri-las após aplicar o ano.')

        seen = set()
        diagnostics = []

        for ano in ANOS:
            modalidades_ano = modalidades
            if modalidades_ano is None:
                probe = consulta.page(ano, 1, modalidade=None, defaults=defaults)
                modalidades_ano = consulta.modalidades(probe)
                if modalidades_ano:
                    print(f'[{ano}] Modalidades descobertas após aplicar o ano: {len(modalidades_ano)}')
                    for _, rotulo in modalidades_ano[:20]:
                        print(f'  - {rotulo}')
                else:
                    default_mod = clean(defaults.get('lic_modalidade'))
                    modalidades_ano = [(default_mod, default_mod)] if default_mod else [(None, 'sem modalidade explícita')]
                    print(f'[{ano}] AVISO: nenhuma modalidade foi identificada; tentando a configuração padrão do formulário.')
            for modalidade, rotulo in modalidades_ano:
                first = consulta.page(ano, 1, modalidade=modalidade, defaults=defaults)
                recs, pages = extract_list(first)
                print(f'[{ano}] {rotulo}: {len(recs)} registro(s) detectado(s) na página 1')

                if not recs:
                    diagnostics.append(str(save_diagnostic(first, ano, modalidade, rotulo)))
                    continue

                maxp = max(pages or [1])
                page = 1
                consecutive_empty = 0
                while page <= max(maxp, 1) + MAX_PAGINAS_SEM_PAGINADOR:
                    if page == 1:
                        rr = recs
                    else:
                        r = consulta.page(ano, page, modalidade=modalidade, defaults=defaults)
                        rr, discovered = extract_list(r)
                        if discovered:
                            maxp = max(maxp, max(discovered))

                    if not rr:
                        consecutive_empty += 1
                        if page > maxp or consecutive_empty >= 1:
                            break
                    else:
                        consecutive_empty = 0

                    for rec in rr:
                        ident = (rec['consulta_id'], rec['consulta_tipo'])
                        if ident in seen:
                            continue
                        seen.add(ident)
                        total += 1
                        dresp = consulta.detail(*ident)
                        detail, ands = extract_detail(dresp)
                        details += 1
                        x = normalized(rec, detail)
                        x['hash_conteudo'] = sha({k: v for k, v in x.items() if k not in {'sincronizado_em', 'hash_conteudo'}})
                        externos.append(x)
                        supa.post('consulta_compras', [x], 'consulta_id,consulta_tipo')
                        # A tabela representa o retrato atual da aba OCORRÊNCIAS.
                        # Remove linhas antigas/extrações equivocadas antes de regravar.
                        supa.delete(
                            'consulta_compras_andamentos',
                            f'consulta_id=eq.{ident[0]}&consulta_tipo=eq.{ident[1]}'
                        )
                        for a in ands:
                            a.update({'consulta_id': ident[0], 'consulta_tipo': ident[1], 'sincronizado_em': nowiso()})
                        if ands:
                            supa.post('consulta_compras_andamentos', ands, 'consulta_id,consulta_tipo,assinatura')
                            andcount += len(ands)
                        print(f"  -> {total}: {x.get('numero_licitacao') or ident} - {x.get('situacao') or ''}")
                        time.sleep(PAUSA)

                    if maxp > 1 and page >= maxp:
                        break
                    page += 1

        if total == 0:
            diag_msg = diagnostics[-1] if diagnostics else str(DIAG_DIR)
            raise RuntimeError(
                'Nenhum registro foi detectado nas páginas do Consulta Compras. '
                f'Foi gerado diagnóstico local em: {diag_msg}. '
                'Envie apenas o arquivo .json de diagnóstico; ele não contém senha, cookie ou chave do Supabase.'
            )

        vinc = match_links(supa, externos)
        auto_novos, auto_atualizados, auto_fora_saude = sync_auto_dispensas(supa, externos)
        if auto_novos or auto_atualizados:
            print(f'Dispensas/Inexigibilidades da Saúde: {auto_novos} nova(s), {auto_atualizados} atualizada(s).')
        print(f'Dispensas/Inexigibilidades fora da Saúde ignoradas: {auto_fora_saude}.')
        if execid:
            supa.patch('integracao_execucoes', f'id=eq.{execid}', {
                'finalizado_em': nowiso(),
                'status': 'sucesso',
                'registros': total,
                'detalhes': details,
                'andamentos': andcount,
                'vinculos': vinc,
                'mensagem': 'Sincronização concluída',
            })
        print(f'Concluído: {total} registros, {andcount} ocorrências/andamentos, {vinc} vínculos.')

    except Exception as e:
        if execid:
            try:
                supa.patch('integracao_execucoes', f'id=eq.{execid}', {
                    'finalizado_em': nowiso(),
                    'status': 'erro',
                    'registros': total,
                    'detalhes': details,
                    'andamentos': andcount,
                    'mensagem': str(e)[:1000],
                })
            except Exception:
                pass
        raise
    finally:
        consulta.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print('ERRO:', e, file=sys.stderr)
        raise SystemExit(1)
