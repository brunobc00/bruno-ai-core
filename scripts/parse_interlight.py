#!/usr/bin/env python3
"""
parse_interlight.py — extrai produtos do PDF de tabela de preços da Interlight para CSV.

Hierarquia do PDF da Interlight:
  [grupo]          ex: "DW 6W e 8W - QUADRADA E REDONDA"
  [categoria]      ex: "Embutir Teto"
  FOTO REF...      → marcador de seção (skip)
  [subseção]       ex: "Embutir Teto - Fixo Potência Total: 8W"
  [cor/NCM]        ex: "Cor do LED: Branco quente - 3000K 9405.11.90"
  [PROD]           ex: "IL DW01 BM Moldura... R$ 194,20 R$ 213,13 9,75%"
  [cont]           ex: "PM Pintura eletrostática... IRC: >82 Cx. 06 peças"
  [cont]           ex: "LED COB e driver... Ângulo de abertura: 24°"
  [cont]           ex: "Med.: 92x92mm... Tensão: 90~240V"

Uso: python3 scripts/parse_interlight.py <arquivo.pdf> [saida.csv]
"""

import csv
import re
import sys
from pathlib import Path

# ── Padrões ───────────────────────────────────────────────────────────────────

_NCM_RE    = re.compile(r'\b\d{4}\.\d{2}\.\d{2}\b')
_PRICE_RE  = re.compile(r'R\$\s*([\d]+(?:\s+[\d]+)*\s*,\s*\d{2})')
_IPI_RE    = re.compile(r'\b(\d{1,2}(?:,\d{1,4})?)\s*%')
_POT_RE    = re.compile(r'Potência\s+[Tt]otal\s*:', re.I)
_COR_RE    = re.compile(r'(?:Cor\s+do\s+LED\s*:|^LED\s+(?:Branco|Amarelo|Verde|Azul|Quente|Frio|Suave|Soft))', re.I | re.M)
_PAGE_RE   = re.compile(r'^\d+\s+IA\d+$')   # rodapé "1 IA0216"

_COLOR_CODES = frozenset({
    'BM','PM','PT','MC','AM','VD','AZ','AP','BK','WH','OW','BR','PR','PB',
    'AT','GD','CP','CR','VS','GE','ND','PX','AB','FE','MD','NE','CB','IE',
    'CST',
})

_HEADER_TOKENS = frozenset({'FOTO','REF','DESCRIÇÃO','OBSERVAÇÕES','PREÇO'})

# Palavras-chave que iniciam a parte técnica (observação) dentro de uma linha
_OBS_SPLIT_RE = re.compile(
    r'(?:'
    r'Fluxo\s+[Ll]uminoso\s*:'
    r'|IRC\s*:'
    r'|[Âa]ngulo\s+de\s+abertura\s*:'
    r'|Tensão\s*:'
    r'|Cx\.\s*\d+'
    r')',
    re.I,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def is_header(line: str) -> bool:
    return len(set(line.upper().split()) & _HEADER_TOKENS) >= 3

def has_price(line: str) -> bool:
    return bool(_PRICE_RE.search(line))

def is_subseção(line: str) -> bool:
    return bool(_POT_RE.search(line))

def is_cor_led(line: str) -> bool:
    return bool(_COR_RE.search(line)) or (bool(_NCM_RE.search(line)) and not has_price(line))

def is_page_footer(line: str) -> bool:
    return bool(_PAGE_RE.match(line)) or re.match(r'^\d{1,2}\s+IA', line) is not None

def parse_float(raw: str) -> float:
    return float(re.sub(r'\s+', '', raw).replace(',', '.'))

def split_obs(text: str) -> tuple[str, str]:
    """Divide texto em (parte_fisica, parte_tecnica) no primeiro keyword de observação."""
    m = _OBS_SPLIT_RE.search(text)
    if m:
        return text[:m.start()].rstrip('. '), text[m.start():]
    return text, ''

def extract_code(prefix: str) -> tuple[str, str]:
    """Extrai código e texto de descrição do prefixo antes do preço."""
    words = prefix.split()
    if not words:
        return '', ''
    ci = next((i for i, w in enumerate(words) if w in _COLOR_CODES), None)
    if ci is not None:
        codigo = ' '.join(words[:ci + 1])
        desc   = ' '.join(words[ci + 1:])
    else:
        ref, rest = [], []
        for i, w in enumerate(words):
            if re.match(r'^[A-Z0-9][A-Z0-9.\-/]*$', w):
                ref.append(w)
            else:
                rest = words[i:]
                break
        codigo = ' '.join(ref) if ref else words[0]
        desc   = ' '.join(rest)
    return codigo.strip(), desc.strip()

def parse_subseção(line: str) -> tuple[str, str]:
    """'Embutir Teto - Fixo Potência Total: 8W' → ('Embutir Teto - Fixo', 'Potência Total: 8W')"""
    m = _POT_RE.search(line)
    if m:
        name = line[:m.start()].strip().rstrip('-').strip()
        pot  = line[m.start():].strip()
        return name, pot
    return line.strip(), ''

def strip_leading_color(line: str) -> str:
    """Remove prefixo de código/cor:
      'PM Pintura...'          → 'Pintura...'
      'IL DB06-AB-W PM Pin...' → 'Pintura...'
      '2712-AB-W Med.:...'     → 'Med.:...'
      'AD 2722-AB-W Med.:...'  → 'Med.:...'
    """
    words = line.split()
    if not words:
        return line

    _code_word = re.compile(r'^[A-Z0-9][A-Z0-9.\-/]*$')

    # Case 1: começa com código de cor simples
    if words[0] in _COLOR_CODES:
        return ' '.join(words[1:])

    # Case 2: ref(s) seguido de código de cor → "IL DB06-AB-W PM Pintura..."
    for i, w in enumerate(words):
        if w in _COLOR_CODES and all(_code_word.match(words[j]) for j in range(i)):
            return ' '.join(words[i + 1:])
        if not _code_word.match(w):
            break

    # Case 3: código de variante com hífen → "2712-AB-W Med.:..." ou "AD 2722-AB-W Med.:..."
    ref_end = 0
    for w in words:
        if _code_word.match(w):
            ref_end += 1
        else:
            break
    if ref_end > 0 and ref_end < len(words):
        candidate = ' '.join(words[:ref_end])
        rest = ' '.join(words[ref_end:])
        if '-' in candidate and rest:   # só strip se houver hífen (é código, não "IP 40")
            return rest

    return line

# ── Parser principal ──────────────────────────────────────────────────────────

def parse_pdf(pdf_path: str) -> list[dict]:
    import pdfplumber

    all_lines: list[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            all_lines.extend((page.extract_text() or '').split('\n'))

    # Classifica cada linha
    classified: list[tuple[str, str]] = []
    for raw in all_lines:
        line = raw.strip()
        if not line or is_page_footer(line):
            classified.append(('empty', line))
        elif is_header(line):
            classified.append(('header', line))
        elif has_price(line):
            classified.append(('product', line))
        elif is_subseção(line):
            classified.append(('subseção', line))
        elif is_cor_led(line):
            classified.append(('cor_led', line))
        else:
            classified.append(('text', line))

    products: list[dict] = []
    n = len(classified)

    grupo      = ''
    categoria  = ''
    sub_desc   = ''
    sub_pot    = ''
    ncm_atual  = ''
    cor_obs    = ''
    pending: dict | None = None
    last_base_code = ''   # código base do último produto real (sem o color code)

    def flush():
        nonlocal pending
        if pending:
            products.append(pending)
        pending = None

    for idx, (typ, line) in enumerate(classified):
        if typ == 'empty':
            continue

        # ── FOTO REF header → captura grupo+categoria das linhas anteriores ──
        if typ == 'header':
            pre = []
            j = idx - 1
            while j >= 0 and len(pre) < 4:
                t, l = classified[j]
                if t == 'text' and l:
                    pre.insert(0, l)
                elif t == 'empty':
                    pass
                else:
                    break
                j -= 1
            # As 2 últimas linhas de texto antes do header = grupo + categoria
            if len(pre) >= 2:
                grupo, categoria = pre[-2], pre[-1]
            elif len(pre) == 1:
                grupo, categoria = pre[0], ''
            ncm_atual      = ''
            cor_obs        = ''
            last_base_code = ''
            continue

        # ── Subseção: "Embutir Teto - Fixo Potência Total: 8W" ───────────────
        if typ == 'subseção':
            flush()
            sub_desc, sub_pot = parse_subseção(line)
            ncm_atual      = ''
            cor_obs        = ''
            last_base_code = ''
            continue

        # ── Linha de cor/NCM ──────────────────────────────────────────────────
        if typ == 'cor_led':
            m = _NCM_RE.search(line)
            if m:
                ncm_atual = m.group(0)
            # Remove o NCM e guarda o restante como texto de observação
            info = _NCM_RE.sub('', line).strip()
            # Remove possível código de referência no início (ex: "2712-FE-W BM Corpo em...")
            info = strip_leading_color(info)
            # Mantém apenas a parte "Cor do LED: X" ou "LED Branco X"
            cor_m = _COR_RE.search(info)
            cor_obs = info[cor_m.start():].strip() if cor_m else info.strip()
            continue

        # ── Linha de produto (tem preço) ──────────────────────────────────────
        if typ == 'product':
            flush()

            # NCM pode estar na linha do produto
            ncm_m = _NCM_RE.search(line)
            ncm_prod = ncm_m.group(0) if ncm_m else ''
            clean = _NCM_RE.sub('', line)

            prices = list(_PRICE_RE.finditer(clean))
            if not prices:
                continue

            prefix = clean[:prices[0].start()].strip()
            codigo, desc_text = extract_code(prefix)

            # Se o código é apenas um color code (ex: "PM", "IE"), herda a base do produto anterior
            if codigo in _COLOR_CODES and last_base_code:
                codigo = f"{last_base_code} {codigo}"
            else:
                # Atualiza a base: remove o último token se for color code
                toks = codigo.split()
                if toks and toks[-1] in _COLOR_CODES:
                    last_base_code = ' '.join(toks[:-1])
                elif toks:
                    last_base_code = codigo

            # Divide desc_text em físico e técnico
            phys_prod, obs_prod = split_obs(desc_text)

            # Preços e IPI
            p1 = parse_float(prices[0].group(1))
            p2 = parse_float(prices[1].group(1)) if len(prices) >= 2 else None
            ipi = None
            for m in _IPI_RE.finditer(clean[prices[0].start():]):
                try:
                    v = float(m.group(1).replace(',', '.'))
                    if 0 < v < 50:          # descarta falsos positivos
                        ipi = v
                        break
                except Exception:
                    pass

            obs_parts  = [x for x in [sub_pot, cor_obs, obs_prod] if x]
            desc_parts = [x for x in [sub_desc, phys_prod] if x]

            pending = {
                'codigo':      codigo,
                'descricao':   sub_desc,
                'desc_parts':  desc_parts,
                'ncm':         ncm_prod or ncm_atual,
                'unidade':     'un',
                'preco_base':  p1,
                'ipi_produto': ipi,
                'preco_cipi':  p2,
                'linha_produto': ' '.join(filter(None, [grupo, categoria])),
                'obs_parts':   obs_parts,
            }
            continue

        # ── Linhas de continuação (após produto) ──────────────────────────────
        if typ == 'text' and pending is not None:
            cont = strip_leading_color(line)
            if not cont:
                continue
            phys, obs = split_obs(cont)
            if phys:
                pending['desc_parts'].append(phys)
            if obs:
                pending['obs_parts'].append(obs)
            continue

    flush()

    # Monta linhas finais
    rows = []
    for p in products:
        if not p['codigo'] or not p['preco_base']:
            continue
        rows.append({
            'codigo':             p['codigo'],
            'descricao':          p['descricao'],
            'descricao_completa': '\n'.join(filter(None, p['desc_parts'])),
            'ncm':                p['ncm'] or '',
            'unidade':            p['unidade'],
            'preco_base':         p['preco_base'],
            'ipi_produto':        p['ipi_produto'] if p['ipi_produto'] is not None else '',
            'preco_cipi':         p['preco_cipi'] or '',
            'linha_produto':      p['linha_produto'],
            'observacao':         '\n'.join(filter(None, p['obs_parts'])),
        })
    return rows


# ── Entrypoint ────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Uso: python3 scripts/parse_interlight.py <arquivo.pdf> [saida.csv]")
        sys.exit(1)

    pdf_path = sys.argv[1]
    csv_path = sys.argv[2] if len(sys.argv) > 2 else str(Path(pdf_path).with_suffix('.csv'))

    rows = parse_pdf(pdf_path)
    print(f"Extraídos: {len(rows)} produtos")

    fields = ['codigo', 'descricao', 'descricao_completa', 'ncm', 'unidade',
              'preco_base', 'ipi_produto', 'preco_cipi', 'linha_produto', 'observacao']

    with open(csv_path, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=fields, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)

    print(f"CSV salvo: {csv_path}")

    print('\n── Primeiros 3 produtos ──')
    for r in rows[:3]:
        print()
        for k, v in r.items():
            vstr = str(v).replace('\n', ' | ')
            print(f"  {k:22}: {vstr[:90]}")


if __name__ == '__main__':
    main()
