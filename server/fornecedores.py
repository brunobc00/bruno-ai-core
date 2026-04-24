import asyncio
import json
import re
import traceback
import uuid
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel

from db import get_session, Fornecedor, TabelaPreco, ProdutoTabela

router = APIRouter(prefix="/api/fornecedores", tags=["fornecedores"])

UPLOADS_DIR = Path(__file__).parent.parent / "data" / "uploads" / "tabelas"
OLLAMA_URL  = "http://localhost:11434"

# ── Parser de texto PDF (regex — sem LLM) ─────────────────────────────────────

_COLOR_CODES = frozenset({
    'BM','PM','PT','MC','AM','VD','AZ','AP','BK','WH','OW','BR','PR','PB','AT','GD','CP','CR'
})
_NCM_RE   = re.compile(r'\b\d{4}\.\d{2}\.\d{2}\b')
_PRICE_RE = re.compile(r'R\$\s*([\d]+\s*(?:\s[\d]+)*\s*,\s*\d{2})')
_SKIP_TOK = frozenset({'FOTO','REF','REF.','COR','DESCRIÇÃO','OBSERVAÇÕES','PREÇO','IPI','NCM'})


def _detect_secao(line: str) -> str | None:
    """Detecta linhas de seção/linha-de-produto acima de grupos de itens no PDF."""
    s = line.strip()
    if len(s) < 8:
        return None
    if _PRICE_RE.search(s):
        return None
    if _NCM_RE.search(s):
        return None
    words = s.split()
    if not words:
        return None
    if words[0].rstrip('.').upper() in _SKIP_TOK:
        return None
    # Rejeita linhas que parecem código de produto solitário (ex: "IL DW01 BM")
    if len(words) <= 2 and all(re.match(r'^[A-Z0-9.\-/]+$', w) for w in words):
        return None
    # Precisa ter pelo menos uma palavra com letras minúsculas ou mista (texto descritivo)
    has_desc = any(re.search(r'[a-záéíóúãõç]', w) for w in words)
    if not has_desc and len(words) < 4:
        return None
    return s


def _parse_pdf_price(raw: str) -> float:
    return float(re.sub(r'\s+', '', raw).replace(',', '.'))


def _parse_pdf_line(line: str) -> dict | None:
    stripped = line.strip()
    if not stripped:
        return None
    if stripped.split()[0].rstrip('.').upper() in _SKIP_TOK:
        return None

    # Captura NCM antes de remover
    ncm_match = _NCM_RE.search(stripped)
    ncm = ncm_match.group(0) if ncm_match else None

    clean = _NCM_RE.sub('', stripped)

    # Encontra todos os preços na linha
    all_prices = list(_PRICE_RE.finditer(clean))
    if not all_prices:
        return None

    m = all_prices[0]  # PREÇO S/IPI
    try:
        preco = _parse_pdf_price(m.group(1))
    except ValueError:
        return None
    if not (0 < preco < 100_000):
        return None

    # PREÇO C/IPI (segundo preço — referência)
    preco_cipi = None
    if len(all_prices) >= 2:
        try:
            preco_cipi = _parse_pdf_price(all_prices[1].group(1))
        except (ValueError, IndexError):
            pass

    # IPI% — padrão X,XX% após os preços (ex: 9,75%)
    ipi_prod = None
    ipi_pct_match = re.search(r'\b(\d+(?:,\d+)?)\s*%', clean[m.start():])
    if ipi_pct_match:
        try:
            ipi_prod = float(ipi_pct_match.group(1).replace(',', '.'))
        except ValueError:
            pass

    prefix = clean[:m.start()].strip()
    words = prefix.split()
    if not words:
        return None
    color_idx = next((i for i, w in enumerate(words) if w in _COLOR_CODES), None)
    if color_idx is not None:
        ref    = ' '.join(words[:color_idx])
        cor    = words[color_idx]
        desc   = ' '.join(words[color_idx + 1:]).strip()
        codigo = f"{ref} {cor}".strip() if ref else cor
    else:
        ref_words: list[str] = []
        desc_words: list[str] = []
        for i, w in enumerate(words):
            if re.match(r'^[A-Z0-9][A-Z0-9.\-/]*$', w):
                ref_words.append(w)
            else:
                desc_words = words[i:]
                break
        if not ref_words:
            ref_words, desc_words = [words[0]], words[1:]
        codigo = ' '.join(ref_words)
        desc   = ' '.join(desc_words).strip()

    return {
        "codigo":             codigo,
        "descricao":          (desc or codigo)[:200],
        "descricao_completa": desc if len(desc) > 200 else None,
        "ncm":                ncm,
        "unidade":            "un",
        "preco_base":         preco,
        "ipi_produto":        ipi_prod,   # IPI% específico do produto no PDF
        "preco_cipi":         preco_cipi, # preço c/ IPI listado no PDF (referência)
    }


# ── Jobs em memória ───────────────────────────────────────────────────────────
# {tid: {"percent": int, "msg": str, "rows": [...], "done": bool, "error": str|None}}
_jobs: dict[int, dict] = {}


# ── Serialização ──────────────────────────────────────────────────────────────

def _f(f: Fornecedor) -> dict:
    return {
        "id":                  f.id,
        "nome":                f.nome,
        "nome_representante":  f.nome_representante,
        "whatsapp":            f.whatsapp,
        "email_cotacao":       f.email_cotacao,
        "email_pedido":        f.email_pedido,
        "contato_nome":        f.contato_nome,
        "contato_tel":         f.contato_tel,
        "contato_email":       f.contato_email,
        "prazo_entrega":       f.prazo_entrega,
        "compra_minima":       float(f.compra_minima) if f.compra_minima else None,
        "cond_pagamento":      f.cond_pagamento,
        "desconto_volume":     json.loads(f.desconto_volume) if f.desconto_volume else None,
        "criado_em":           f.criado_em.isoformat() if f.criado_em else None,
    }


def _t(t: TabelaPreco, com_produtos: bool = False) -> dict:
    d = {
        "id":            t.id,
        "fornecedor_id": t.fornecedor_id,
        "data_upload":   t.data_upload.isoformat() if t.data_upload else None,
        "arquivo_nome":  t.arquivo_nome,
        "arquivo_tipo":  t.arquivo_tipo,
        "desconto":      float(t.desconto or 0),
        "ipi":           float(t.ipi or 0),
        "icms_entrada":  float(t.icms_entrada or 0),
        "st":            float(t.st or 0),
        "status":        t.status,
        "total_produtos": len(t.produtos),
    }
    if com_produtos:
        d["produtos"] = [_p(p) for p in t.produtos]
    return d


def _p(p: ProdutoTabela) -> dict:
    return {
        "id":                  p.id,
        "codigo":              p.codigo,
        "descricao":           p.descricao,
        "descricao_completa":  p.descricao_completa,
        "observacao":          p.observacao,
        "ncm":                 p.ncm,
        "unidade":             p.unidade,
        "preco_base":          float(p.preco_base)      if p.preco_base      else None,
        "preco_desconto":      float(p.preco_desconto)  if p.preco_desconto  else None,
        "preco_custo":         float(p.preco_custo)     if p.preco_custo     else None,
        "ipi":                 float(p.ipi)             if p.ipi             else None,
        "icms_entrada":        float(p.icms_entrada)    if p.icms_entrada    else None,
        "st":                  float(p.st)              if p.st              else None,
        "linha_produto":       p.linha_produto,
        "descricao_generica":  p.descricao_generica,
        "url_produto":         p.url_produto,
        "imagens":             json.loads(p.imagens) if p.imagens else [],
    }


def _calc(preco_base: float, desconto: float, ipi: float, st: float) -> tuple[float, float]:
    preco_desc  = round(preco_base * (1 - desconto / 100), 4)
    preco_custo = round(preco_desc * (1 + ipi / 100) * (1 + st / 100), 4)
    return preco_desc, preco_custo


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ── Fornecedores CRUD ─────────────────────────────────────────────────────────

class FornecedorIn(BaseModel):
    nome:               str
    nome_representante: Optional[str]   = None
    whatsapp:           Optional[str]   = None
    email_cotacao:      Optional[str]   = None
    email_pedido:       Optional[str]   = None
    contato_nome:       Optional[str]   = None
    contato_tel:        Optional[str]   = None
    contato_email:      Optional[str]   = None
    prazo_entrega:      Optional[int]   = None
    compra_minima:      Optional[float] = None
    cond_pagamento:     Optional[str]   = None
    desconto_volume:    Optional[dict]  = None


@router.get("")
def list_fornecedores():
    with get_session() as db:
        rows = db.query(Fornecedor).order_by(Fornecedor.nome).all()
        return [_f(r) for r in rows]


@router.post("", status_code=201)
def create_fornecedor(body: FornecedorIn):
    with get_session() as db:
        f = Fornecedor(
            nome=body.nome,
            nome_representante=body.nome_representante,
            whatsapp=body.whatsapp,
            email_cotacao=body.email_cotacao,
            email_pedido=body.email_pedido,
            contato_nome=body.contato_nome,
            contato_tel=body.contato_tel,
            contato_email=body.contato_email,
            prazo_entrega=body.prazo_entrega,
            compra_minima=body.compra_minima,
            cond_pagamento=body.cond_pagamento,
            desconto_volume=json.dumps(body.desconto_volume) if body.desconto_volume else None,
        )
        db.add(f)
        db.commit()
        db.refresh(f)
        return _f(f)


@router.get("/{fid}")
def get_fornecedor(fid: int):
    with get_session() as db:
        f = db.get(Fornecedor, fid)
        if not f:
            raise HTTPException(404, "Fornecedor não encontrado")
        return _f(f)


@router.put("/{fid}")
def update_fornecedor(fid: int, body: FornecedorIn):
    with get_session() as db:
        f = db.get(Fornecedor, fid)
        if not f:
            raise HTTPException(404, "Fornecedor não encontrado")
        for k, v in body.model_dump(exclude_none=True).items():
            setattr(f, k, json.dumps(v) if k == "desconto_volume" else v)
        db.commit()
        db.refresh(f)
        return _f(f)


@router.delete("/{fid}", status_code=204)
def delete_fornecedor(fid: int):
    with get_session() as db:
        f = db.get(Fornecedor, fid)
        if not f:
            raise HTTPException(404, "Fornecedor não encontrado")
        db.delete(f)
        db.commit()


# ── Tabelas de Preço ──────────────────────────────────────────────────────────

@router.get("/{fid}/tabelas")
def list_tabelas(fid: int):
    with get_session() as db:
        if not db.get(Fornecedor, fid):
            raise HTTPException(404, "Fornecedor não encontrado")
        rows = (db.query(TabelaPreco)
                  .filter_by(fornecedor_id=fid)
                  .order_by(TabelaPreco.data_upload.desc())
                  .all())
        return [_t(r) for r in rows]


@router.post("/{fid}/tabelas", status_code=201)
async def upload_tabela(
    fid: int,
    arquivo:      UploadFile = File(...),
    desconto:     float = Form(0),
    ipi:          float = Form(0),
    icms_entrada: float = Form(0),
    st:           float = Form(0),
):
    with get_session() as db:
        if not db.get(Fornecedor, fid):
            raise HTTPException(404, "Fornecedor não encontrado")

    ext  = Path(arquivo.filename or "").suffix.lower().lstrip(".")
    tipo = ext if ext in ("pdf", "xls", "xlsx", "txt", "jpg", "jpeg", "png") else "outro"

    dest = UPLOADS_DIR / str(fid)
    dest.mkdir(parents=True, exist_ok=True)
    nome_salvo = f"{uuid.uuid4().hex}_{arquivo.filename}"
    arquivo_path = dest / nome_salvo
    arquivo_path.write_bytes(await arquivo.read())

    with get_session() as db:
        t = TabelaPreco(
            fornecedor_id=fid,
            arquivo_nome=arquivo.filename,
            arquivo_path=str(arquivo_path),
            arquivo_tipo=tipo,
            desconto=desconto,
            ipi=ipi,
            icms_entrada=icms_entrada,
            st=st,
            status="aguardando",
        )
        db.add(t)
        db.commit()
        db.refresh(t)
        tid       = t.id
        arq_path  = t.arquivo_path
        arq_tipo  = t.arquivo_tipo
        resultado = _t(t)

    # Auto-inicia processamento em background
    _jobs[tid] = {"percent": 0, "msg": "Aguardando...", "rows": [], "done": False, "error": None}
    asyncio.create_task(_run_job(tid, arq_path, arq_tipo))
    return resultado


@router.get("/{fid}/tabelas/{tid}/arquivo")
def download_arquivo(fid: int, tid: int):
    from fastapi.responses import FileResponse
    with get_session() as db:
        t = db.get(TabelaPreco, tid)
        if not t or t.fornecedor_id != fid:
            raise HTTPException(404, "Tabela não encontrada")
        path = t.arquivo_path
        nome = t.arquivo_nome or "arquivo"
    if not path or not Path(path).exists():
        raise HTTPException(404, "Arquivo não encontrado no servidor")
    return FileResponse(path, filename=nome, media_type="application/octet-stream")


@router.get("/{fid}/tabelas/{tid}")
def get_tabela(fid: int, tid: int):
    with get_session() as db:
        t = db.get(TabelaPreco, tid)
        if not t or t.fornecedor_id != fid:
            raise HTTPException(404, "Tabela não encontrada")
        return _t(t, com_produtos=True)


@router.delete("/{fid}/tabelas/{tid}", status_code=204)
def delete_tabela(fid: int, tid: int):
    with get_session() as db:
        t = db.get(TabelaPreco, tid)
        if not t or t.fornecedor_id != fid:
            raise HTTPException(404, "Tabela não encontrada")
        if t.arquivo_path and Path(t.arquivo_path).exists():
            Path(t.arquivo_path).unlink(missing_ok=True)
        db.delete(t)
        db.commit()


class ProdutoIn(BaseModel):
    codigo:    Optional[str]   = None
    descricao: str
    unidade:   Optional[str]   = None
    preco_base: float


@router.post("/{fid}/tabelas/{tid}/produtos")
def salvar_produtos(fid: int, tid: int, produtos: list[ProdutoIn]):
    with get_session() as db:
        t = db.get(TabelaPreco, tid)
        if not t or t.fornecedor_id != fid:
            raise HTTPException(404, "Tabela não encontrada")

        for p in t.produtos:
            db.delete(p)

        desc = float(t.desconto or 0)
        ipi  = float(t.ipi or 0)
        st   = float(t.st or 0)

        for item in produtos:
            pd, pc = _calc(item.preco_base, desc, ipi, st)
            db.add(ProdutoTabela(
                tabela_id=tid,
                codigo=item.codigo,
                descricao=item.descricao,
                unidade=item.unidade,
                preco_base=item.preco_base,
                preco_desconto=pd,
                preco_custo=pc,
                ipi=t.ipi,
                icms_entrada=t.icms_entrada,
                st=t.st,
            ))

        t.status = "processado"
        db.commit()
        return {"ok": True, "total": len(produtos)}


# ── Rotas globais (produtos / tabelas) ───────────────────────────────────────

from fastapi import Query as QParam
from sqlalchemy import or_

_global_router = APIRouter(tags=["fornecedores-global"])


@_global_router.get("/api/tabelas")
def list_all_tabelas():
    with get_session() as db:
        rows = (
            db.query(TabelaPreco, Fornecedor)
            .join(Fornecedor, TabelaPreco.fornecedor_id == Fornecedor.id)
            .order_by(TabelaPreco.data_upload.desc())
            .all()
        )
        result = []
        for t, f in rows:
            d = _t(t)
            d["fornecedor_nome"] = f.nome
            result.append(d)
        return result


@_global_router.get("/api/produtos")
def search_produtos(q: str = QParam(default="")):
    with get_session() as db:
        query = (
            db.query(ProdutoTabela, TabelaPreco, Fornecedor)
            .join(TabelaPreco, ProdutoTabela.tabela_id == TabelaPreco.id)
            .join(Fornecedor, TabelaPreco.fornecedor_id == Fornecedor.id)
        )
        if q:
            like = f"%{q}%"
            query = query.filter(
                or_(
                    ProdutoTabela.codigo.ilike(like),
                    ProdutoTabela.descricao.ilike(like),
                    ProdutoTabela.descricao_generica.ilike(like),
                )
            )
        rows = query.order_by(ProdutoTabela.descricao).limit(500).all()
        result = []
        for p, t, f in rows:
            d = _p(p)
            d["fornecedor_nome"] = f.nome
            d["tabela_data"]     = t.data_upload.isoformat() if t.data_upload else None
            result.append(d)
        return result


class BulkGenericaIn(BaseModel):
    ids:                list[int]
    descricao_generica: str


@_global_router.patch("/api/produtos/bulk-generica")
def bulk_generica(body: BulkGenericaIn):
    with get_session() as db:
        rows = db.query(ProdutoTabela).filter(ProdutoTabela.id.in_(body.ids)).all()
        for p in rows:
            p.descricao_generica = body.descricao_generica
        db.commit()
        return {"ok": True, "updated": len(rows)}


# ── SSE: processar arquivo ────────────────────────────────────────────────────

async def _stream_xls(arquivo_path: str):
    import pandas as pd
    try:
        df = pd.read_excel(arquivo_path, header=None, dtype=str)
    except Exception as e:
        yield ("error", str(e))
        return

    total = len(df)
    count = 0
    for _, row in df.iterrows():
        vals = [str(v).strip() for v in row if str(v).strip() not in ("", "nan", "None")]
        if len(vals) < 2:
            continue
        descricao = vals[0]
        preco = None
        for v in vals[1:]:
            try:
                preco = float(v.replace("R$", "").replace(".", "").replace(",", ".").strip())
                break
            except ValueError:
                continue
        if descricao and preco and preco > 0:
            count += 1
            pct = int(10 + 85 * count / max(total, 1))
            yield ("row",    {"descricao": descricao, "preco_base": preco})
            yield ("status", {"msg": f"{count} produto(s)...", "percent": pct})
    yield ("done", {})


async def _stream_pdf_texto_llm(arquivo_path: str):
    try:
        import pdfplumber
        pages_text: list[str] = []
        with pdfplumber.open(arquivo_path) as pdf:
            n_pages = len(pdf.pages)
            for page in pdf.pages:
                pages_text.append(page.extract_text() or "")
    except Exception as e:
        yield ("error", f"{type(e).__name__}: {e}")
        return

    if not any(t.strip() for t in pages_text):
        yield ("empty", {})
        return

    count = 0
    secao_atual = None  # header de seção acima do grupo de produtos
    pendente: dict | None = None  # produto aguardando possível observação da linha seguinte
    for i, page_text in enumerate(pages_text):
        pct = int(10 + 75 * i / n_pages)
        yield ("status", {"msg": f"Analisando pág. {i+1}/{n_pages}...", "percent": pct})
        for line in page_text.split('\n'):
            prod = _parse_pdf_line(line)
            if prod:
                if pendente is not None:
                    count += 1
                    yield ("row", pendente)
                if secao_atual:
                    prod['linha_produto'] = secao_atual
                pendente = prod
            else:
                stripped = line.strip()
                # Captura a primeira linha sem preço após produto como observação,
                # antes de tentar detectar seção (a seção real virá em linha seguinte)
                if (pendente is not None
                        and not pendente.get('observacao')
                        and stripped
                        and stripped.split()[0].rstrip('.').upper() not in _SKIP_TOK):
                    pendente['observacao'] = stripped
                else:
                    sec = _detect_secao(line)
                    if sec:
                        secao_atual = sec
        # Emite produto pendente ao fim da página
        if pendente is not None:
            count += 1
            yield ("row", pendente)
            pendente = None
        yield ("status", {"msg": f"Pág. {i+1}/{n_pages} — {count} produto(s)", "percent": pct})
        await asyncio.sleep(0)

    if count == 0:
        yield ("empty", {})
    else:
        yield ("done", {})


async def _stream_ollama_vision(arquivo_path: str, tipo: str):
    import base64
    import fitz  # PyMuPDF

    img_paths: list[str] = []

    if tipo == "pdf":
        doc = fitz.open(arquivo_path)
        for i, page in enumerate(doc):
            pix = page.get_pixmap(dpi=200)
            p   = Path(arquivo_path).parent / f"_tmp_{uuid.uuid4().hex}_p{i}.jpg"
            pix.save(str(p))
            img_paths.append(str(p))
    else:
        img_paths = [arquivo_path]

    n = len(img_paths)
    count = 0

    prompt = (
        "Extraia todos os produtos desta tabela de preços. "
        "Para cada produto retorne UMA linha JSON com os campos: "
        '{"codigo": "...", "descricao": "...", "unidade": "...", "preco_base": 0.00}\n'
        "Somente linhas JSON, uma por linha, sem texto adicional. "
        "Valores numéricos sem R$ ou vírgulas — use ponto decimal."
    )

    for idx, img_path in enumerate(img_paths):
        yield ("status", {"msg": f"Ollama analisando página {idx+1}/{n}...", "percent": int(10 + 75 * idx / n)})

        with open(img_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()

        payload = {
            "model":  "llama3.2-vision",
            "prompt": prompt,
            "images": [img_b64],
            "stream": True,
        }

        buffer = ""
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                async with client.stream("POST", f"{OLLAMA_URL}/api/generate", json=payload) as resp:
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        try:
                            chunk = json.loads(line)
                            if chunk.get("error"):
                                msg = f"llama3.2-vision: {chunk['error']}"
                                print(f"[VISION] Ollama erro: {msg}")
                                yield ("error", msg)
                                return
                            buffer += chunk.get("response", "")
                            while "\n" in buffer:
                                ln, buffer = buffer.split("\n", 1)
                                ln = ln.strip()
                                if ln.startswith("{") and ln.endswith("}"):
                                    try:
                                        produto = json.loads(ln)
                                        if produto.get("descricao"):
                                            count += 1
                                            yield ("row",    produto)
                                            yield ("status", {"msg": f"{count} produto(s) extraído(s)...", "percent": min(int(10 + 75 * idx / n) + count, 90)})
                                    except json.JSONDecodeError:
                                        pass
                        except json.JSONDecodeError:
                            pass
        except Exception as e:
            msg = f"llama3.2-vision — {type(e).__name__}: {e}"
            print(f"[VISION] ERRO página {idx+1}: {msg}\n{traceback.format_exc()}")
            yield ("error", msg)
            return
        finally:
            if tipo == "pdf":
                Path(img_path).unlink(missing_ok=True)

    yield ("done", {})


async def _stream_txt(arquivo_path: str):
    conteudo = Path(arquivo_path).read_text(encoding="utf-8", errors="replace")[:8000]
    payload = {
        "model":  "qwen2.5:7b",
        "prompt": (
            f"Extraia todos os produtos desta tabela de preços:\n\n{conteudo}\n\n"
            'Para cada produto retorne UMA linha JSON: {"codigo":"...","descricao":"...","unidade":"...","preco_base":0.00}\n'
            "Somente JSON, um por linha, sem texto adicional. Valores numéricos com ponto decimal."
        ),
        "stream": True,
    }

    buffer = ""
    count  = 0
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            async with client.stream("POST", f"{OLLAMA_URL}/api/generate", json=payload) as resp:
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        chunk  = json.loads(line)
                        buffer += chunk.get("response", "")
                        while "\n" in buffer:
                            ln, buffer = buffer.split("\n", 1)
                            ln = ln.strip()
                            if ln.startswith("{") and ln.endswith("}"):
                                try:
                                    produto = json.loads(ln)
                                    if produto.get("descricao"):
                                        count += 1
                                        yield ("row",    produto)
                                        yield ("status", {"msg": f"{count} produto(s)...", "percent": min(10 + count * 3, 90)})
                                except json.JSONDecodeError:
                                    pass
                    except json.JSONDecodeError:
                        pass
    except Exception as e:
        yield ("error", str(e))
        return

    yield ("done", {})


async def _run_job(tid: int, arquivo_path: str, arquivo_tipo: str):
    job = _jobs[tid]
    print(f"[JOB] tid={tid} tipo={arquivo_tipo} iniciando")

    def _set(percent=None, msg=None):
        if percent is not None: job["percent"] = percent
        if msg is not None:
            job["msg"] = msg
            print(f"[JOB] tid={tid} {percent or '?'}% — {msg}")

    # Carrega parâmetros fiscais e limpa produtos antigos
    with get_session() as db:
        t = db.get(TabelaPreco, tid)
        if not t:
            job["error"] = "Tabela não encontrada"; job["done"] = True; return
        t.status = "processando"
        for p in t.produtos:
            db.delete(p)
        db.commit()
        fid      = t.fornecedor_id
        arq_nome = t.arquivo_nome or ""
        desc_pct = float(t.desconto      or 0)
        ipi_pct  = float(t.ipi           or 0)
        icms_val = float(t.icms_entrada  or 0)
        st_pct   = float(t.st            or 0)

    def _fail(msg: str):
        job["error"] = msg
        job["done"]  = True
        print(f"[JOB] tid={tid} ERRO: {msg}")
        with get_session() as db:
            t = db.get(TabelaPreco, tid)
            if t: t.status = "erro"; db.commit()

    _set(0, "Iniciando...")

    try:
        gen = None

        if arquivo_tipo in ("xls", "xlsx"):
            _set(5, "Lendo planilha...")
            gen = _stream_xls(arquivo_path)

        elif arquivo_tipo == "pdf":
            _set(5, "Analisando PDF...")
            rows_found = False
            async for ev, data in _stream_pdf_texto_llm(arquivo_path):
                if ev == "row":
                    rows_found = True
                    job["rows"].append(data)
                elif ev == "status":
                    _set(data.get("percent"), data.get("msg"))
                elif ev == "empty":
                    _set(15, "Sem texto detectado — usando visão do Ollama...")
                    gen = _stream_ollama_vision(arquivo_path, "pdf")
                    break
                elif ev == "error":
                    _fail(data); return
                elif ev == "done":
                    if rows_found: break

        elif arquivo_tipo in ("jpg", "jpeg", "png"):
            _set(5, "Enviando imagem para Ollama...")
            gen = _stream_ollama_vision(arquivo_path, arquivo_tipo)

        elif arquivo_tipo == "txt":
            _set(5, "Processando texto com Ollama...")
            gen = _stream_txt(arquivo_path)

        if gen:
            async for ev, data in gen:
                if ev == "row":
                    job["rows"].append(data)
                elif ev == "status":
                    _set(data.get("percent"), data.get("msg"))
                elif ev == "error":
                    _fail(data); return
                elif ev == "done":
                    break

    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        print(f"[JOB] ERRO tid={tid}: {msg}\n{traceback.format_exc()}")
        _fail(msg); return

    total = len(job["rows"])
    print(f"[JOB] tid={tid} EXTRAÍDO — {total} produto(s), aguardando revisão")
    _set(100, f"Aguardando revisão — {total} produto(s) extraídos.")
    job["done"] = True

    # Salva arquivo de revisão (não vai para o banco ainda)
    review_path = UPLOADS_DIR / str(fid) / f"{tid}_review.json"
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(json.dumps({
        "tabela_id":   tid,
        "arquivo":     arq_nome,
        "desconto":    desc_pct,
        "ipi":         ipi_pct,
        "icms_entrada": icms_val,
        "st":          st_pct,
        "produtos":    job["rows"],
    }, ensure_ascii=False, indent=2))

    with get_session() as db:
        t = db.get(TabelaPreco, tid)
        if t: t.status = "revisao"; db.commit()


@router.post("/{fid}/tabelas/{tid}/processar", status_code=202)
async def iniciar_processamento(fid: int, tid: int):
    with get_session() as db:
        t = db.get(TabelaPreco, tid)
        if not t or t.fornecedor_id != fid:
            raise HTTPException(404, "Tabela não encontrada")
        arquivo_path = t.arquivo_path
        arquivo_tipo = t.arquivo_tipo

    if not arquivo_path or not Path(arquivo_path).exists():
        raise HTTPException(400, "Arquivo não encontrado no servidor")

    _jobs[tid] = {"percent": 0, "msg": "Aguardando...", "rows": [], "done": False, "error": None}
    asyncio.create_task(_run_job(tid, arquivo_path, arquivo_tipo))
    return {"ok": True, "tid": tid}


@router.get("/{fid}/tabelas/{tid}/review")
def get_review(fid: int, tid: int):
    with get_session() as db:
        t = db.get(TabelaPreco, tid)
        if not t or t.fornecedor_id != fid:
            raise HTTPException(404, "Tabela não encontrada")
        fid_real = t.fornecedor_id
    review_path = UPLOADS_DIR / str(fid_real) / f"{tid}_review.json"
    if not review_path.exists():
        raise HTTPException(404, "Revisão não disponível")
    return json.loads(review_path.read_text())


class _ReviewBody(BaseModel):
    produtos: list[dict]


@router.put("/{fid}/tabelas/{tid}/review")
def put_review(fid: int, tid: int, body: _ReviewBody):
    with get_session() as db:
        t = db.get(TabelaPreco, tid)
        if not t or t.fornecedor_id != fid:
            raise HTTPException(404)
        fid_real = t.fornecedor_id
    review_path = UPLOADS_DIR / str(fid_real) / f"{tid}_review.json"
    if not review_path.exists():
        raise HTTPException(404, "Revisão não disponível")
    data = json.loads(review_path.read_text())
    data["produtos"] = body.produtos
    review_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    return {"ok": True, "total": len(body.produtos)}


@router.post("/{fid}/tabelas/{tid}/importar", status_code=200)
def importar_tabela(fid: int, tid: int):
    with get_session() as db:
        t = db.get(TabelaPreco, tid)
        if not t or t.fornecedor_id != fid:
            raise HTTPException(404)
        review_path = UPLOADS_DIR / str(t.fornecedor_id) / f"{tid}_review.json"
        if not review_path.exists():
            raise HTTPException(400, "Nenhuma extração pendente para importar")
        data     = json.loads(review_path.read_text())
        desc_pct = float(t.desconto      or 0)
        ipi_pct  = float(t.ipi           or 0)
        icms_val = float(t.icms_entrada  or 0)
        st_pct   = float(t.st            or 0)
        for p in t.produtos:
            db.delete(p)
        count = 0
        for prod in data.get("produtos", []):
            raw = prod.get("preco_base")
            if not prod.get("descricao") or raw is None:
                continue
            try:
                preco = float(str(raw).replace(",", "."))
            except (ValueError, TypeError):
                continue
            if not (0 < preco < 100_000):
                continue
            # Usa IPI/ST do produto quando definidos individualmente
            ipi_prod = prod.get("ipi_produto")
            st_prod  = prod.get("st_produto")
            ipi_usar = float(ipi_prod) if ipi_prod is not None else ipi_pct
            st_usar  = float(st_prod)  if st_prod  is not None else st_pct
            pd, pc = _calc(preco, desc_pct, ipi_usar, st_usar)
            imgs = prod.get("imagens")
            db.add(ProdutoTabela(
                tabela_id=tid,
                codigo=prod.get("codigo", ""),
                descricao=prod["descricao"],
                descricao_completa=prod.get("descricao_completa"),
                linha_produto=prod.get("linha_produto"),
                observacao=prod.get("observacao"),
                ncm=prod.get("ncm"),
                unidade=prod.get("unidade", "un"),
                preco_base=preco,
                preco_desconto=pd,
                preco_custo=pc,
                ipi=ipi_usar,
                icms_entrada=icms_val,
                st=st_usar,
                url_produto=prod.get("url_produto"),
                imagens=json.dumps(imgs) if isinstance(imgs, list) else imgs,
            ))
            count += 1
        t.status = "processado"
        db.commit()
    print(f"[IMPORTAR] tid={tid} — {count} produto(s) importados para o banco")
    return {"ok": True, "total": count}


@router.get("/{fid}/tabelas/{tid}/progresso")
def get_progresso(fid: int, tid: int, offset: int = 0):
    with get_session() as db:
        t = db.get(TabelaPreco, tid)
        if not t or t.fornecedor_id != fid:
            raise HTTPException(404, "Tabela não encontrada")

    job = _jobs.get(tid)
    if not job:
        return {"percent": 0, "msg": "Não iniciado", "rows": [], "done": False, "error": None}

    return {
        "percent": job["percent"],
        "msg":     job["msg"],
        "rows":    job["rows"][offset:],
        "done":    job["done"],
        "error":   job["error"],
    }
