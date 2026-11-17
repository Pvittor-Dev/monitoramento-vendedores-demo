"""Relatórios em PDF para os supervisores.

    diario   positivação do dia por vendedor, até o momento, separada por mesa
    semanal  rascunho do relatório da semana: média de positivações, quadro por mesa,
             pontos de atenção e destaques calculados pelas regras abaixo

Uso:
    python relatorios.py diario                                  # hoje, até agora
    python relatorios.py diario --data 2026-11-13
    python relatorios.py semanal                                 # semana passada, segunda a sexta
    python relatorios.py semanal --inicio 2026-11-09 --fim 2026-11-13
    --abrir abre o PDF no fim

Saídas na pasta saida/ (fora do git):
    positivacao_AAAA-MM-DD.pdf
    semanal_AAAA-MM-DD_a_AAAA-MM-DD.pdf  e  .txt (os pontos em texto, para colar no seu relatório)

Mesmas regras do painel (gerar_painel.py): positivação = cliente com pedido do vendedor no dia.
A mesa é o código do supervisor no cadastro (supervisor 10 = Mesa 100).
"""
from __future__ import annotations

import argparse
import base64
import html
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import gerar_painel as gp

# ---------------------------------------------------------------------------
# Regras dos pontos de atenção do semanal. Mudar aqui muda o relatório.
# ---------------------------------------------------------------------------
PAROU_CEDO = "14:00"        # última saída antes desse horário = parou as visitas logo após o almoço
PAROU_CEDO_DIAS = 2         # ... em pelo menos 2 dias da semana
FORA_ROTA_PCT = 20          # clientes visitados fora da rota do dia: a partir de 20% dos visitados
FORA_ROTA_MIN = 5           # ... e pelo menos 5 na semana
SEM_CHECKIN_DIAS = 2        # dias com pedido e nenhuma entrada no GPS
# Entrada longe e visita relâmpago aparecem em quase todos os vendedores (ver o painel), então ficam
# fora dos pontos de atenção. Para ligar, troque None por um número mínimo na semana.
LONGE_MIN = None
RELAMPAGO_MIN = None
BAIXA_COBERTURA = 0.75      # positivação da rota abaixo de 75% da média da mesa
DESTAQUES_POR_MESA = 2
PREVISTOS_MIN_DESTAQUE = 20  # só entra em destaque quem teve pelo menos 20 clientes previstos na semana
ATIVO_DIAS = 30             # o vendedor aparece se teve pedido ou visita nos últimos 30 dias
SUPERVISORES_FORA = {99}    # Superv. Broker (códigos 900 e 999)
SEM_ALERTA = ("clientes estr",)  # códigos de carteira da casa (Clientes Estratégicos): sem alerta nem destaque

DIAS_SEMANA = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


# ---------------------------------------------------------------------------
# Dados
# ---------------------------------------------------------------------------
def carregar(inicio: date, fim: date) -> dict:
    ate = fim + timedelta(days=1)
    desde = fim - timedelta(days=ATIVO_DIAS - 1)
    with gp.conectar(gp.ler_env(gp.RAIZ / ".env")) as cn:
        visitas = gp.consultar(cn, "visitas.sql", inicio, ate)
        pedidos = gp.consultar(cn, "pedidos.sql", inicio, ate)
        clientes = gp.consultar(cn, "clientes.sql", inicio, ate, inicio, ate)
        vendedores = gp.consultar(cn, "vendedores.sql")
        motivos = gp.consultar(cn, "motivos.sql")
        rota = gp.consultar(cn, "rota.sql")
        ciclos = gp.consultar(cn, "ciclos.sql", inicio, ate)
        ativos = {r["vendedor"] for r in gp.consultar(cn, "ativos.sql", desde, ate, desde, ate)}

    base = gp.montar(visitas, pedidos, clientes, vendedores, motivos)
    ultimo_checkin = max((v["entrada"] for v in visitas if v["entrada"]), default=None)

    # Registro do painel por (dia, vendedor), com os conjuntos de clientes
    dia_vend = {}
    for r in base["registros"]:
        visitados = {d[0] for d in r["vis"]}
        positivados = {d[0] for d in r["vis"] if d[7]} | {s[0] for s in r["sv"]}
        dia_vend[(date.fromisoformat(r["d"]), r["v"])] = {"m": r["m"], "vis": visitados, "pos": positivados}

    cadastro = {}
    for e in vendedores:
        sup = e["cod_supervisor"]
        if e["vendedor"] not in ativos or sup in SUPERVISORES_FORA or sup is None:
            continue
        cadastro[e["vendedor"]] = {
            "nome": nome_curto(e["nome"]),
            "mesa": int(sup) * 10,
            "sup": " ".join((e["supervisor"] or "").title().split()),
        }
    return {"dia_vend": dia_vend, "rota": rota, "ciclos": ciclos, "vendedores": cadastro,
            "ultimo_checkin": ultimo_checkin}


def nome_curto(nome: str | None) -> str:
    """'FULANO DE TAL SILVA' -> 'Fulano Tal'."""
    partes = [p for p in (nome or "").strip().title().split() if p.lower() not in ("de", "da", "do", "dos", "das")]
    return " ".join(partes[:2]).strip(" -.")


def previstos_do_dia(rota: list[dict], ciclos: list[dict], dia: date) -> dict[int, set]:
    """Clientes da rota de cada vendedor no dia: dia da semana certo e semana do ciclo certa."""
    ciclo = next((c["ciclo"] for c in ciclos if para_data(c["inicio"]) <= dia <= para_data(c["fim"])), None)
    previstos = defaultdict(set)
    for r in rota:
        if r["dia"] == dia.isoweekday() and (ciclo is None or str(ciclo) in (r["ciclo"] or "")):
            previstos[r["vendedor"]].add(r["pdv"])
    return previstos


def para_data(valor) -> date:
    return valor.date() if isinstance(valor, datetime) else valor if isinstance(valor, date) else date.fromisoformat(str(valor)[:10])


def linha_dia(dados: dict, previstos: dict, dia: date, vendedor: int) -> dict:
    reg = dados["dia_vend"].get((dia, vendedor))
    prev = previstos.get(vendedor, set())
    vis = reg["vis"] if reg else set()
    pos = reg["pos"] if reg else set()
    m = reg["m"] if reg else {}
    return {
        "prev": len(prev), "vis": len(vis), "vis_rota": len(vis & prev), "fora": len(vis - prev),
        "pos": len(pos), "pos_rota": len(pos & prev), "semv": m.get("semv", 0),
        "ini": m.get("ini", ""), "fim": m.get("fim", ""), "visitas": m.get("vis", 0),
        "longe": m.get("longe", 0), "rel": m.get("rel", 0),
    }


def por_mesa(vendedores: dict) -> dict[int, list[int]]:
    mesas = defaultdict(list)
    for cod, v in sorted(vendedores.items()):
        mesas[v["mesa"]].append(cod)
    return dict(sorted(mesas.items()))


def pct(parte: int, todo: int) -> float | None:
    return parte / todo if todo else None


# ---------------------------------------------------------------------------
# HTML e PDF
# ---------------------------------------------------------------------------
CSS = """
@page{size:A4;margin:12mm 11mm 14mm}
*{box-sizing:border-box}
body{margin:0;font:10.5px/1.4 'Segoe UI',Arial,sans-serif;color:#152033;-webkit-print-color-adjust:exact;print-color-adjust:exact}
.topo{display:flex;align-items:center;gap:14px;background:linear-gradient(120deg,#0e1a2b,#16314f);color:#fff;
  padding:12px 16px;border-radius:10px;margin-bottom:12px}
.topo img{height:34px;background:#fff;border-radius:6px;padding:3px 6px}
.topo h1{margin:0;font-size:17px;letter-spacing:.3px}
.topo p{margin:2px 0 0;color:#b9cbe2;font-size:11px}
.topo .dir{margin-left:auto;text-align:right}
.topo .dir b{display:block;font-size:22px;line-height:1.1}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:12px}
.kpi{border:1px solid #dfe6f0;border-radius:8px;padding:8px 10px;border-top:3px solid #0b7abf}
.kpi span{color:#5f6f86;font-size:9.5px;text-transform:uppercase;letter-spacing:.4px}
.kpi b{display:block;font-size:18px;margin-top:2px}
.mesa{break-inside:avoid;margin-bottom:12px}
.mesa h2{display:flex;align-items:baseline;gap:8px;margin:0 0 5px;font-size:13px;color:#0e1a2b;
  border-bottom:2px solid #0b7abf;padding-bottom:3px}
.mesa h2 small{font-weight:400;color:#5f6f86;font-size:10.5px}
.mesa h2 .tot{margin-left:auto;font-size:10.5px;color:#0b7abf}
table{width:100%;border-collapse:collapse}
th{background:#eef3f9;color:#3d4d63;font-weight:600;font-size:9px;text-transform:uppercase;letter-spacing:.3px;
  padding:4px 5px;text-align:right;border-bottom:1px solid #d5deea}
td{padding:3.5px 5px;text-align:right;border-bottom:1px solid #edf1f6}
th:first-child,td:first-child{text-align:left}
tr:nth-child(even) td{background:#fafcfe}
tr.total td{font-weight:700;background:#eef3f9;border-top:1px solid #c8d4e3}
td.zero{color:#b0b9c6}
.barra{display:inline-flex;align-items:center;gap:5px;justify-content:flex-end;width:100%}
.barra i{display:inline-block;height:6px;border-radius:3px;background:#0b7abf}
.barra s{display:inline-block;width:46px;height:6px;border-radius:3px;background:#e4ebf3;text-decoration:none;position:relative;overflow:hidden}
.barra s i{position:absolute;left:0;top:0}
.al{color:#c0392b;font-weight:600}
ul{margin:4px 0 0;padding-left:16px}
li{margin:2px 0}
li b{color:#0e1a2b}
.ok li::marker{content:'★  ';color:#d18a0b}
.sub{font-size:10.5px;font-weight:600;color:#3d4d63;margin:7px 0 2px}
.nada{color:#5f6f86;font-style:italic}
.nota{margin-top:10px;color:#5f6f86;font-size:9px;border-top:1px solid #e2e8f0;padding-top:6px}
"""


def barra(valor: float | None) -> str:
    if valor is None:
        return '<span class="zero">–</span>'
    largura = max(0, min(100, round(valor * 100)))
    return f'<span class="barra"><s><i style="width:{largura}%"></i></s>{valor:.0%}</span>'


def num(n: int) -> str:
    return f'<span class="zero">0</span>' if n == 0 else str(n)


def topo(titulo: str, sub: str, destaque_valor: str = "", destaque_txt: str = "") -> str:
    logo = gp.RAIZ / "logo.png"
    img = f'<img src="data:image/png;base64,{base64.b64encode(logo.read_bytes()).decode()}">' if logo.exists() else ""
    dir_ = f'<div class="dir"><b>{destaque_valor}</b><p>{destaque_txt}</p></div>' if destaque_valor else ""
    return f'<div class="topo">{img}<div><h1>{titulo}</h1><p>{sub}</p></div>{dir_}</div>'


def pagina(titulo: str, corpo: str) -> str:
    return (f'<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><title>{html.escape(titulo)}</title>'
            f'<style>{CSS}</style></head><body>{corpo}</body></html>')


def edge() -> str:
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            caminho = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if caminho.exists():
                return str(caminho)
    sys.exit("Não achei o Microsoft Edge, que é usado para gerar o PDF.")


def gravar_pdf(conteudo: str, pdf: Path) -> None:
    """Grava o HTML ao lado do PDF e imprime com o Edge sem janela."""
    origem = pdf.with_suffix(".html")
    origem.write_text(conteudo, encoding="utf-8")
    with tempfile.TemporaryDirectory() as perfil:  # perfil separado: não mexe no Edge aberto
        subprocess.run([edge(), "--headless", "--disable-gpu", f"--user-data-dir={perfil}",
                        "--no-pdf-header-footer", "--print-to-pdf-no-header",
                        f"--print-to-pdf={pdf}", origem.as_uri()],
                       check=True, timeout=180, capture_output=True)
    origem.unlink(missing_ok=True)
    if not pdf.exists():
        sys.exit(f"O Edge não gerou o PDF {pdf.name}.")


# ---------------------------------------------------------------------------
# Diário
# ---------------------------------------------------------------------------
def diario(dia: date) -> Path:
    dados = carregar(dia, dia)
    previstos = previstos_do_dia(dados["rota"], dados["ciclos"], dia)
    hoje = dia == date.today()

    secoes, geral = [], defaultdict(int)
    for mesa, codigos in por_mesa(dados["vendedores"]).items():
        linhas, tot = [], defaultdict(int)
        for cod in codigos:
            l = linha_dia(dados, previstos, dia, cod)
            if not (l["prev"] or l["vis"] or l["pos"]):
                continue  # sem rota hoje e sem movimento: não entra
            for k in ("prev", "vis", "vis_rota", "fora", "pos", "pos_rota", "semv"):
                tot[k] += l[k]
            nome = dados["vendedores"][cod]["nome"]
            linhas.append(
                f'<tr><td>{cod} – {html.escape(nome)}</td><td>{num(l["prev"])}</td><td>{num(l["vis"])}</td>'
                f'<td>{num(l["fora"])}</td><td><b>{num(l["pos"])}</b></td><td>{num(l["semv"])}</td>'
                f'<td>{barra(pct(l["pos_rota"], l["prev"]))}</td><td>{l["ini"] or "–"}</td><td>{l["fim"] or "–"}</td></tr>')
        if not linhas:
            continue
        for k, v in tot.items():
            geral[k] += v
        sup = dados["vendedores"][codigos[0]]["sup"]
        secoes.append(
            f'<div class="mesa"><h2>Mesa {mesa} <small>Supervisor(a): {html.escape(sup)}</small>'
            f'<span class="tot">{tot["pos"]} positivados</span></h2><table>'
            '<tr><th>Vendedor</th><th>Rota do dia</th><th>Visitados</th><th>Fora da rota</th><th>Positivados</th>'
            '<th>Pedido sem visita</th><th>Positivação da rota</th><th>1ª entrada</th><th>Últ. saída</th></tr>'
            + "".join(linhas) +
            f'<tr class="total"><td>Total da mesa</td><td>{tot["prev"]}</td><td>{tot["vis"]}</td><td>{tot["fora"]}</td>'
            f'<td>{tot["pos"]}</td><td>{tot["semv"]}</td><td>{barra(pct(tot["pos_rota"], tot["prev"]))}</td>'
            '<td></td><td></td></tr></table></div>')

    agora = datetime.now()
    momento = f"até {agora:%H:%M}" if hoje else "dia fechado"
    ult = dados["ultimo_checkin"]
    rodape = (f"Última entrada recebido: {ult:%H:%M}. " if hoje and ult else "") + (
        "Positivados = clientes com pedido do vendedor no dia. Positivação da rota = clientes da rota do dia com "
        "pedido ÷ clientes previstos na rota. Fora da rota = clientes visitados que não estavam na rota do dia. "
        f"Gerado em {agora:%d/%m/%Y %H:%M}.")
    kpis = (
        '<div class="kpis">'
        f'<div class="kpi"><span>Positivados</span><b>{geral["pos"]}</b></div>'
        f'<div class="kpi"><span>Clientes visitados</span><b>{geral["vis"]}</b></div>'
        f'<div class="kpi"><span>Positivação da rota</span><b>{fmt_pct(pct(geral["pos_rota"], geral["prev"]))}</b></div>'
        f'<div class="kpi"><span>Visitas fora da rota</span><b>{geral["fora"]}</b></div></div>')
    corpo = (topo("Positivação do dia", f"{DIAS_SEMANA[dia.weekday()].capitalize()}, {dia:%d/%m/%Y} · {momento}",
                  str(geral["pos"]), "positivados")
             + kpis + "".join(secoes) + f'<p class="nota">{rodape}</p>')

    gp.PASTA_SAIDA.mkdir(exist_ok=True)
    pdf = gp.PASTA_SAIDA / f"positivacao_{dia.isoformat()}.pdf"
    gravar_pdf(pagina(f"Positivação {dia:%d/%m/%Y}", corpo), pdf)
    return pdf


def dec(valor: float) -> str:
    return f"{valor:.1f}".replace(".", ",")


def fmt_pct(valor: float | None) -> str:
    return "–" if valor is None else f"{valor:.0%}"


# ---------------------------------------------------------------------------
# Semanal
# ---------------------------------------------------------------------------
def resumo_semana(dados: dict, inicio: date, fim: date, modo: str = "semana", parcial: bool = False) -> dict:
    """Números, pontos de atenção e destaques da semana (ou de um dia, modo="dia"), em estrutura simples.

    No modo dia os limites são menores (1 dia só): parou cedo e sem entrada valem com 1 dia, fora de rota a partir
    de 3 clientes e destaque a partir de 8 previstos. Com parcial=True (hoje, dia em andamento) "parou cedo" não
    é avaliado, porque o vendedor ainda pode estar trabalhando.
    """
    dia_ = modo == "dia"
    lim_fora_min = 3 if dia_ else FORA_ROTA_MIN
    lim_cedo = (10 ** 6 if parcial else 1) if dia_ else PAROU_CEDO_DIAS
    lim_semck = 1 if dia_ else SEM_CHECKIN_DIAS
    lim_prev_dest = 8 if dia_ else PREVISTOS_MIN_DESTAQUE
    dias = [inicio + timedelta(days=i) for i in range((fim - inicio).days + 1)]
    uteis = [d for d in dias if d.weekday() < 5]
    previstos = {d: previstos_do_dia(dados["rota"], dados["ciclos"], d) for d in uteis}

    # Média de positivações diárias: todos os vendedores, dias úteis (mesma conta do painel)
    pos_dia = defaultdict(int)
    for (d, _v), reg in dados["dia_vend"].items():
        if d in previstos:
            pos_dia[d] += len(reg["pos"])
    dias_com_venda = [d for d in uteis if pos_dia[d]]
    media = sum(pos_dia.values()) / len(dias_com_venda) if dias_com_venda else 0

    # Resumo por vendedor
    resumo = {}
    for cod in dados["vendedores"]:
        s = defaultdict(int)
        s["cedo"], s["sem_ck"] = [], []
        for d in uteis:
            l = linha_dia(dados, previstos[d], d, cod)
            for k in ("prev", "vis", "fora", "pos", "pos_rota", "longe", "rel"):
                s[k] += l[k]
            if l["vis"] or l["pos"]:
                s["dias"] += 1
            if l["visitas"] and l["fim"] and l["fim"] < PAROU_CEDO:
                s["cedo"].append(f'{DIAS_SEMANA[d.weekday()]} {l["fim"]}')
            if l["pos"] and not l["visitas"]:
                s["sem_ck"].append(DIAS_SEMANA[d.weekday()])
        s["cob"] = pct(s["pos_rota"], s["prev"])
        s["media"] = s["pos"] / len(dias_com_venda) if dias_com_venda else 0
        resumo[cod] = s

    tot_prev = tot_posr = 0
    if dia_:
        mesas, texto = [], ["RESUMO DO DIA", f"Dia {inicio:%d/%m/%Y}",
                            f"{sum(pos_dia.values())} positivações no dia" + (" (dia em andamento)" if parcial else ""), ""]
    else:
        mesas, texto = [], ["RELATÓRIO SEMANAL", f"Semana do dia {inicio:%d/%m} até {fim:%d/%m}",
                            f"Média de {media:.0f} positivações diárias", ""]
    for mesa, codigos in por_mesa(dados["vendedores"]).items():
        codigos = [c for c in codigos if resumo[c]["prev"] or resumo[c]["dias"]]
        if not codigos:
            continue
        cob_mesa = pct(sum(resumo[c]["pos_rota"] for c in codigos), sum(resumo[c]["prev"] for c in codigos))
        media_mesa = sum(resumo[c]["pos"] for c in codigos) / len(dias_com_venda) if dias_com_venda else 0

        vend, pontos, destaques = [], [], []
        for cod in codigos:
            s, nome = resumo[cod], dados["vendedores"][cod]["nome"]
            fora_pct = pct(s["fora"], s["vis"])
            alerta = []
            if any(n in nome.lower() for n in SEM_ALERTA):
                pass
            elif not s["dias"]:
                alerta.append(f"sem visitas e sem pedidos {'no dia' if dia_ else 'na semana'}, com {s['prev']} clientes previstos na rota. "
                              "Conferir afastamento ou rota sem vendedor")
            else:
                if fora_pct is not None and fora_pct * 100 >= FORA_ROTA_PCT and s["fora"] >= lim_fora_min:
                    alerta.append(f"grande volume de atendimentos fora de rota: {s['fora']} de {s['vis']} "
                                  f"clientes visitados ({fora_pct:.0%})")
                if len(s["cedo"]) >= lim_cedo:
                    if dia_:
                        alerta.append(f"parou os atendimentos presenciais cedo: última saída às {s['cedo'][0].split()[-1]} "
                                      f"(antes das {PAROU_CEDO})")
                    else:
                        alerta.append(f"parou os atendimentos presenciais antes das {PAROU_CEDO} em {len(s['cedo'])} "
                                      f"dias (última saída: {', '.join(s['cedo'])})")
                if len(s["sem_ck"]) >= lim_semck:
                    if dia_:
                        alerta.append("pedido no dia e nenhuma entrada no GPS: aparelho sem sincronizar ou venda sem visita")
                    else:
                        alerta.append(f"{len(s['sem_ck'])} dias com pedido e nenhuma entrada no GPS "
                                      f"({', '.join(s['sem_ck'])}): aparelho sem sincronizar ou venda sem visita")
                if s["cob"] is not None and cob_mesa and s["cob"] < cob_mesa * BAIXA_COBERTURA:
                    alerta.append(f"positivação abaixo da mesa: {s['cob']:.0%} da rota positivada "
                                  f"(mesa: {cob_mesa:.0%})")
                if LONGE_MIN is not None and s["longe"] >= LONGE_MIN:
                    alerta.append(f"{s['longe']} entradas a mais de {gp.DISTANCIA_LONGE_M} m do endereço do cliente")
                if RELAMPAGO_MIN is not None and s["rel"] >= RELAMPAGO_MIN:
                    alerta.append(f"{s['rel']} visitas com menos de {gp.VISITA_RELAMPAGO_SEG // 60} minutos no cliente")
            if alerta:
                pontos.append((cod, nome, alerta))
            vend.append({"cod": cod, "nome": nome, "dias": s["dias"], "prev": s["prev"], "vis": s["vis"],
                         "fora_pct": fora_pct, "pos": s["pos"], "media": s["media"], "cob": s["cob"],
                         "cedo": len(s["cedo"]), "sem_ck": len(s["sem_ck"]), "alerta": bool(alerta)})

        candidatos = [c for c in codigos if resumo[c]["prev"] >= lim_prev_dest and resumo[c]["cob"] is not None
                      and cob_mesa and resumo[c]["cob"] >= cob_mesa
                      and not any(n in dados["vendedores"][c]["nome"].lower() for n in SEM_ALERTA)]
        for cod in sorted(candidatos, key=lambda c: -resumo[c]["cob"])[:DESTAQUES_POR_MESA]:
            destaques.append((cod, dados["vendedores"][cod]["nome"], resumo[cod]["cob"]))

        tot_prev += sum(resumo[c]["prev"] for c in codigos)
        tot_posr += sum(resumo[c]["pos_rota"] for c in codigos)
        mesas.append({"mesa": mesa, "sup": dados["vendedores"][codigos[0]]["sup"], "media": media_mesa,
                      "cob": cob_mesa, "vend": vend,
                      "pontos": [[c, n, a] for c, n, a in pontos],
                      "destaques": [[c, n, cob] for c, n, cob in destaques]})

        texto.append(f"Mesa {mesa}")
        texto += [f"- {n} – {c}: {'; '.join(a)}." for c, n, a in pontos] or ["- Nenhum ponto de atenção."]
        if destaques:
            texto.append("- Vendedores em destaque:")
            texto += [f"  • {n} ({c}) -> {cob:.0%} da rota positivada" for c, n, cob in destaques]
        texto.append("")

    return {"modo": modo, "parcial": parcial, "prev": tot_prev, "posr": tot_posr,
            "cob": tot_posr / tot_prev if tot_prev else None,
            "inicio": inicio.isoformat(), "fim": fim.isoformat(), "media": media,
            "total": sum(pos_dia.values()), "dias_venda": len(dias_com_venda),
            "alertas": sum(1 for m in mesas for v in m["vend"] if v["alerta"]),
            "mesas": mesas, "texto": "\n".join(texto)}


def semanal(inicio: date, fim: date) -> tuple[Path, Path]:
    r = resumo_semana(carregar(inicio, fim), inicio, fim)
    secoes = []
    for m in r["mesas"]:
        linhas = "".join(
            f'<tr><td class="{"al" if v["alerta"] else ""}">{v["cod"]} – {html.escape(v["nome"])}</td><td>{v["dias"]}</td>'
            f'<td>{num(v["prev"])}</td><td>{num(v["vis"])}</td><td>{fmt_pct(v["fora_pct"])}</td>'
            f'<td><b>{num(v["pos"])}</b></td><td>{dec(v["media"])}</td><td>{barra(v["cob"])}</td>'
            f'<td>{num(v["cedo"])}</td><td>{num(v["sem_ck"])}</td></tr>' for v in m["vend"])
        lis = "".join(f'<li><b>{html.escape(n)} – {c}:</b> {"; ".join(a)}.</li>' for c, n, a in m["pontos"]) \
            or '<li class="nada">Nenhum ponto de atenção pelas regras do relatório.</li>'
        des = "".join(f'<li>{html.escape(n)} ({c}): {cob:.0%} da rota positivada</li>' for c, n, cob in m["destaques"]) \
            or '<li class="nada">Ninguém acima da média da mesa com rota suficiente.</li>'
        secoes.append(
            f'<div class="mesa"><h2>Mesa {m["mesa"]} <small>Supervisor(a): {html.escape(m["sup"])}</small>'
            f'<span class="tot">{m["media"]:.0f} positivações/dia · {fmt_pct(m["cob"])} da rota</span></h2><table>'
            '<tr><th>Vendedor</th><th>Dias</th><th>Rota prevista</th><th>Visitados</th><th>Fora da rota</th>'
            '<th>Positivados</th><th>Média/dia</th><th>Positivação da rota</th><th>Parou cedo</th>'
            '<th>Sem entrada</th></tr>' + linhas + '</table>'
            f'<p class="sub">Pontos de atenção</p><ul>{lis}</ul>'
            f'<p class="sub">Destaques da semana</p><ul class="ok">{des}</ul></div>')

    agora = datetime.now()
    nota = ("Rascunho gerado pelas regras do monitoramento; os comentários sobre conversas com supervisores ficam "
            "por sua conta. Positivação da rota = clientes da rota com pedido ÷ clientes previstos na rota (dias "
            f"úteis). Parou cedo = última saída antes das {PAROU_CEDO}. Sem entrada = dia com pedido e nenhuma "
            f"visita no GPS. A rota é a do cadastro atual. Gerado em {agora:%d/%m/%Y %H:%M}.")
    kpis = (
        '<div class="kpis">'
        f'<div class="kpi"><span>Média de positivações/dia</span><b>{r["media"]:.0f}</b></div>'
        f'<div class="kpi"><span>Positivações na semana</span><b>{r["total"]}</b></div>'
        f'<div class="kpi"><span>Dias com venda</span><b>{r["dias_venda"]}</b></div>'
        f'<div class="kpi"><span>Vendedores com alerta</span><b>{r["alertas"]}</b></div>'
        '</div>')
    corpo = (topo("Relatório semanal", f"Semana do dia {inicio:%d/%m} até {fim:%d/%m/%Y}",
                  f"{r['media']:.0f}", "positivações por dia")
             + kpis + "".join(secoes) + f'<p class="nota">{nota}</p>')

    gp.PASTA_SAIDA.mkdir(exist_ok=True)
    nome = f"semanal_{inicio.isoformat()}_a_{fim.isoformat()}"
    pdf, txt = gp.PASTA_SAIDA / f"{nome}.pdf", gp.PASTA_SAIDA / f"{nome}.txt"
    gravar_pdf(pagina(f"Relatório semanal {inicio:%d/%m} a {fim:%d/%m}", corpo), pdf)
    txt.write_text(r["texto"], encoding="utf-8-sig")
    return pdf, txt


def semanas_do_periodo(inicio: date, fim: date) -> list[tuple[date, date]]:
    """Semanas (segunda a sexta) que cabem inteiras no período, da mais recente para a mais antiga."""
    seg = inicio + timedelta(days=(7 - inicio.weekday()) % 7)
    semanas = []
    while seg + timedelta(days=4) <= fim:
        semanas.append((seg, seg + timedelta(days=4)))
        seg += timedelta(days=7)
    return semanas[::-1]


DIAS_NO_PAINEL = 15   # quantos dias úteis recentes levam o resumo do dia no painel


def resumos_painel(inicio: date, fim: date) -> tuple[list[dict], list[dict]]:
    """(semanas completas, dias úteis recentes) do período, com uma única leitura do banco."""
    semanas = semanas_do_periodo(inicio, fim)
    uteis = [inicio + timedelta(days=i) for i in range((fim - inicio).days + 1)]
    uteis = [d for d in uteis if d.weekday() < 5][-DIAS_NO_PAINEL:]
    if not semanas and not uteis:
        return [], []
    dados = carregar(inicio, fim)
    dias = [resumo_semana(dados, d, d, "dia", parcial=(d == date.today())) for d in reversed(uteis)]
    dias = [x for x in dias if x["total"] or x["prev"]]
    return [resumo_semana(dados, i, f) for i, f in semanas], dias


def resumos_para_painel(inicio: date, fim: date) -> list[dict]:
    """Resumo de cada semana fechada do período, para a visão Resumo do painel."""
    semanas = semanas_do_periodo(inicio, fim)
    if not semanas:
        return []
    dados = carregar(semanas[-1][0], semanas[0][1])
    return [resumo_semana(dados, i, f) for i, f in semanas]


# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Relatórios em PDF para os supervisores.")
    sub = ap.add_subparsers(dest="tipo", required=True)
    d = sub.add_parser("diario", help="positivação do dia por vendedor, até o momento")
    d.add_argument("--data", type=date.fromisoformat, default=date.today(), help="AAAA-MM-DD (padrão: hoje)")
    s = sub.add_parser("semanal", help="rascunho do relatório da semana")
    s.add_argument("--inicio", type=date.fromisoformat, help="AAAA-MM-DD (padrão: segunda da semana passada)")
    s.add_argument("--fim", type=date.fromisoformat, help="AAAA-MM-DD (padrão: sexta da semana passada)")
    for p in (d, s):
        p.add_argument("--abrir", action="store_true", help="abre o PDF no fim")
    args = ap.parse_args()

    if args.tipo == "diario":
        arquivos = [diario(args.data)]
    else:
        seg = date.today() - timedelta(days=date.today().weekday() + 7)
        inicio = args.inicio or seg
        fim = args.fim or inicio + timedelta(days=4)
        if inicio > fim:
            sys.exit("A data inicial é depois da final.")
        arquivos = list(semanal(inicio, fim))

    for a in arquivos:
        print(f"Gerado: {a}")
    if args.abrir:
        os.startfile(arquivos[0])  # noqa: S606 (Windows)


if __name__ == "__main__":
    main()
