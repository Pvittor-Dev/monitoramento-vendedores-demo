"""Gera o painel de monitoramento de vendedores.

Cruza as visitas do GPS (entrada no cliente) com os pedidos do sistema de pedidos.
Positivação = cliente com pelo menos um pedido do vendedor no dia (tabela PEDIDO).

Uso:
    python gerar_painel.py                                # últimos 31 dias até hoje
    python gerar_painel.py --dias 7
    python gerar_painel.py --inicio 2026-11-01 --fim 2026-11-16
    python gerar_painel.py --abrir                        # abre o painel no fim

Saídas (pasta saida/, fora do git):
    painel.html               painel com filtros, tabela por vendedor e detalhe das visitas
    vendedor_dia.csv          um registro por vendedor e dia, para abrir no Excel
"""
from __future__ import annotations

import argparse
import base64
import csv
import json
import math
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import pyodbc

RAIZ = Path(__file__).resolve().parent
PASTA_SQL = RAIZ / "sql"
PASTA_SAIDA = RAIZ / "saida"

# ---------------------------------------------------------------------------
# Regras do monitoramento. Mudar aqui muda o painel inteiro.
# ---------------------------------------------------------------------------
VISITA_RELAMPAGO_SEG = 120      # visita com menos de 2 minutos no cliente
DISTANCIA_LONGE_M = 300         # entrada a mais de 300 m da coordenada do cadastro do cliente
TIPO_MOTIVO_NAO_COMPRA = 2
DIAS_PREVISTOS_NO_MAPA = 14     # só os últimos N dias levam a lista de previstos sem visita (para o HTML não crescer demais)      # MOTIVO.COD_TIPO_MOTIVO de "Nao Positivacao de Cliente"


# ---------------------------------------------------------------------------
# Banco
# ---------------------------------------------------------------------------
def nome_sup(e: dict, vendedores: list[dict]) -> str:
    """Nome do supervisor. Se o mesmo nome aparece em duas mesas (ex.: 300 e 400), acrescenta '(mesa)' para
    o painel tratar cada mesa separada; o painel tira esse sufixo na hora de mostrar."""
    nome = (e["supervisor"] or "Sem supervisor").strip()
    cods = {x["cod_supervisor"] for x in vendedores if (x["supervisor"] or "").strip() == nome and x["cod_supervisor"] is not None}
    return f"{nome} ({int(e['cod_supervisor']) * 10})" if len(cods) > 1 and e["cod_supervisor"] is not None else nome


def ler_env(caminho: Path) -> dict[str, str]:
    """Lê o .env (CHAVE=valor por linha; # é comentário)."""
    if not caminho.exists():
        sys.exit(f"Arquivo {caminho.name} não encontrado. Copie o .env.example para .env e preencha.")
    env = {}
    for linha in caminho.read_text(encoding="utf-8-sig").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, valor = linha.split("=", 1)
        env[chave.strip()] = valor.strip()
    faltando = [c for c in ("DB_SERVER", "DB_DATABASE", "DB_USER", "DB_PASSWORD") if not env.get(c)]
    if faltando:
        sys.exit(f"Preencha no .env: {', '.join(faltando)}")
    return env


def conectar(env: dict[str, str]) -> pyodbc.Connection:
    drivers = [d for d in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server") if d in pyodbc.drivers()]
    if not drivers:
        sys.exit("Instale o 'ODBC Driver 18 for SQL Server' da Microsoft.")

    def sim_nao(valor: str | None, padrao: str) -> str:
        return "yes" if str(valor or padrao).strip().lower() in ("true", "yes", "1", "sim") else "no"

    def protegido(valor: str) -> str:  # chaves protegem ; e = dentro do valor
        return "{" + valor.replace("}", "}}") + "}"

    servidor = env["DB_SERVER"].strip()
    if re.fullmatch(r"[^:,]+:\d+", servidor):  # host:porta -> host,porta (formato do ODBC)
        servidor = servidor.replace(":", ",")

    texto = (
        f"DRIVER={{{drivers[0]}}};SERVER={servidor};DATABASE={env['DB_DATABASE']};"
        f"UID={protegido(env['DB_USER'])};PWD={protegido(env['DB_PASSWORD'])};"
        f"Encrypt={sim_nao(env.get('DB_ENCRYPT'), 'true')};"
        f"TrustServerCertificate={sim_nao(env.get('DB_TRUST_CERT'), 'true')}"
    )
    return pyodbc.connect(texto, timeout=30, readonly=True)


def consultar(cn: pyodbc.Connection, arquivo: str, *params) -> list[dict]:
    sql = (PASTA_SQL / arquivo).read_text(encoding="utf-8")
    cur = cn.cursor()
    cur.execute(sql, *params)
    colunas = [c[0] for c in cur.description]
    return [dict(zip(colunas, linha)) for linha in cur.fetchall()]


# ---------------------------------------------------------------------------
# Cálculos
# ---------------------------------------------------------------------------
def coordenada(valor) -> float | None:
    """Converte a latitude/longitude do banco (texto) em número. Vazio, '-' ou zero viram None."""
    if valor is None:
        return None
    try:
        numero = float(str(valor).strip().replace(",", "."))
    except ValueError:
        return None
    return None if numero == 0 or abs(numero) > 180 else numero


def distancia_m(lat1, lon1, lat2, lon2) -> int | None:
    """Distância em metros entre dois pontos (fórmula de haversine)."""
    if None in (lat1, lon1, lat2, lon2):
        return None
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(a)))


def formata_cnpj(valor) -> str:
    """14 dígitos viram 00.000.000/0000-00, 11 viram CPF 000.000.000-00; outro tamanho fica como veio."""
    d = re.sub(r"\D", "", str(valor or ""))
    if len(d) == 14:
        return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
    if len(d) == 11:
        return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"
    return d


def hhmm(momento: datetime | None) -> str:
    return momento.strftime("%H:%M") if momento else ""


def montar(visitas, pedidos, clientes, vendedores, motivos) -> dict:
    """Junta visitas e pedidos por vendedor e dia e calcula os indicadores."""
    cad_cliente = {}
    cad_doc = {}  # cliente -> (razão social, CNPJ formatado)
    for c in clientes:
        cad_doc[c["pdv"]] = ((c["razao"] or "").strip(), formata_cnpj(c["cnpj"]))
        cad_cliente[c["pdv"]] = (
            (c["fantasia"] or "").strip(),
            coordenada(c["lat"]),
            coordenada(c["lon"]),
        )

    # Pedidos por (data, vendedor) -> cliente
    ped = defaultdict(dict)
    for p in pedidos:
        reg = ped[(p["data"], p["vendedor"])].setdefault(p["pdv"], {"n": 0, "valor": 0.0, "origens": set()})
        reg["n"] += 1
        reg["valor"] += float(p["valor"] or 0)
        reg["origens"].add("nula" if p["origem"] is None else str(p["origem"]))

    # Visitas por (data, vendedor)
    vis = defaultdict(list)
    for v in visitas:
        vis[(v["data"], v["vendedor"])].append(v)

    registros = []
    for data, vendedor in sorted(set(vis) | set(ped)):
        lista = vis.get((data, vendedor), [])
        clientes_ped = ped.get((data, vendedor), {})

        clientes_vis = {v["pdv"] for v in lista}
        com_pocket = {v["pdv"] for v in lista if v["pedido_pocket"] is not None}
        motivos_dia = Counter()
        detalhe = []
        relampago = longe = sem_gps = 0
        tempo_cliente = 0
        for v in lista:
            dur = None
            if v["entrada"] and v["saida"] and v["saida"] >= v["entrada"]:
                dur = int((v["saida"] - v["entrada"]).total_seconds())
                tempo_cliente += dur
                if dur < VISITA_RELAMPAGO_SEG:
                    relampago += 1
            nome, lat_c, lon_c = cad_cliente.get(v["pdv"], ("", None, None))
            lat_v, lon_v = coordenada(v["lat"]), coordenada(v["lon"])
            dist = distancia_m(lat_v, lon_v, lat_c, lon_c)
            if dist is None:
                sem_gps += 1
            elif dist > DISTANCIA_LONGE_M:
                longe += 1
            cod_mot = v["cod_motivo"] if v["tipo_motivo"] == TIPO_MOTIVO_NAO_COMPRA and v["cod_motivo"] else 0
            if cod_mot:
                motivos_dia[cod_mot] += 1
            detalhe.append([
                v["pdv"], hhmm(v["entrada"]), hhmm(v["saida"]),
                -1 if dur is None else dur,
                cod_mot,
                1 if v["pedido_pocket"] is not None else 0,
                -1 if dist is None else dist,
                1 if v["pdv"] in clientes_ped else 0,
                None if lat_v is None else round(lat_v, 5),
                None if lon_v is None else round(lon_v, 5),
            ])

        positivados = set(clientes_ped)
        entradas = [v["entrada"] for v in lista if v["entrada"]]
        saidas = [v["saida"] or v["entrada"] for v in lista if v["saida"] or v["entrada"]]
        ini, fim = (min(entradas), max(saidas)) if entradas else (None, None)
        sem_visita = sorted(positivados - clientes_vis)

        registros.append({
            "d": data.isoformat(),
            "v": vendedor,
            "m": {
                "vis": len(lista),                                         # visitas (linhas)
                "cli": len(clientes_vis),                                  # clientes visitados
                "pos": len(positivados),                                   # clientes positivados (com pedido)
                "posv": len(positivados & clientes_vis),                   # positivados que tiveram visita
                "semv": len(sem_visita),                                   # positivados sem visita
                "nvp": len((positivados & clientes_vis) - com_pocket),     # visita só com motivo, mas tem pedido
                "rel": relampago,
                "longe": longe,
                "semgps": sem_gps,
                "ini": hhmm(ini),
                "fim": hhmm(fim),
                "jor": round((fim - ini).total_seconds() / 60) if ini else 0,
                "tcli": round(tempo_cliente / 60),
                "ped": sum(r["n"] for r in clientes_ped.values()),
                "val": round(sum(r["valor"] for r in clientes_ped.values()), 2),
                "mot": dict(motivos_dia),
            },
            "vis": detalhe,
            "sv": [[pdv, clientes_ped[pdv]["n"], round(clientes_ped[pdv]["valor"], 2),
                    ",".join(sorted(clientes_ped[pdv]["origens"]))] for pdv in sem_visita],
        })

    usados_vend = {r["v"] for r in registros}
    usados_cli = {d[0] for r in registros for d in r["vis"]} | {s[0] for r in registros for s in r["sv"]}
    return {
        "vendedores": {
            e["vendedor"]: {"nome": (e["nome"] or "").strip(), "sup": nome_sup(e, vendedores),
                                   "cs": e["cod_supervisor"]}
            for e in vendedores if e["vendedor"] in usados_vend
        },
        # cliente -> [nome, latitude, longitude, razão social, CNPJ] (coordenada do cadastro, para o mapa)
        "clientes": {pdv: [cad_cliente.get(pdv, ("", None, None))[0],
                           *(None if c is None else round(c, 5) for c in cad_cliente.get(pdv, ("", None, None))[1:]),
                           *cad_doc.get(pdv, ("", ""))]
                     for pdv in usados_cli},
        "motivos": {m["cod_motivo"]: (m["motivo"] or "").strip()
                    for m in motivos if m["tipo_motivo"] == TIPO_MOTIVO_NAO_COMPRA},
        "registros": registros,
    }


# ---------------------------------------------------------------------------
# Saídas
# ---------------------------------------------------------------------------
def gravar_csv(dados: dict, caminho: Path) -> None:
    campos = ["data", "cod_vendedor", "vendedor", "supervisor", "clientes_previstos", "clientes_visitados", "previstos_pendentes", "visitados_fora_da_rota", "visitas",
              "positivados", "positivados_com_visita", "positivados_sem_visita", "nao_venda_com_pedido",
              "visitas_relampago", "entrada_longe", "visitas_sem_gps", "primeira_entrada", "ultima_saida",
              "jornada_min", "tempo_em_cliente_min", "pedidos", "valor"]
    with caminho.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(campos)
        for r in dados["registros"]:
            m, vend = r["m"], dados["vendedores"].get(r["v"], {"nome": "", "sup": ""})
            w.writerow([r["d"], r["v"], vend["nome"], vend["sup"], m["prev"], m["cli"], m["pend"], m["fora"], m["vis"], m["pos"], m["posv"],
                        m["semv"], m["nvp"], m["rel"], m["longe"], m["semgps"], m["ini"], m["fim"], m["jor"],
                        m["tcli"], m["ped"], str(m["val"]).replace(".", ",")])


def gravar_html(dados: dict, caminho: Path) -> None:
    modelo = (RAIZ / "painel_template.html").read_text(encoding="utf-8")
    logo = RAIZ / "logo.png"
    logo_uri = "data:image/png;base64," + base64.b64encode(logo.read_bytes()).decode() if logo.exists() else ""
    json_dados = json.dumps(dados, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    texto = modelo.replace("__LOGO__", logo_uri).replace("__DADOS__", json_dados)
    # grava num arquivo temporário e troca no fim: quem está lendo o painel nunca pega o arquivo pela metade
    temp = caminho.with_name(caminho.name + ".tmp")
    temp.write_text(texto, encoding="utf-8")
    for tentativa in range(10):
        try:
            os.replace(temp, caminho)
            return
        except PermissionError:  # o servidor local ainda está lendo o arquivo antigo
            time.sleep(0.5)
    sys.exit(f"Não consegui gravar {caminho.name}: o arquivo está em uso.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Gera o painel de monitoramento de vendedores.")
    ap.add_argument("--inicio", type=date.fromisoformat, help="data inicial AAAA-MM-DD")
    ap.add_argument("--fim", type=date.fromisoformat, help="data final AAAA-MM-DD (padrão: hoje)")
    ap.add_argument("--dias", type=int, default=31, help="quantos dias até a data final (padrão: 31)")
    ap.add_argument("--abrir", action="store_true", help="abre o painel no navegador no fim")
    args = ap.parse_args()

    fim = args.fim or date.today()
    inicio = args.inicio or fim - timedelta(days=args.dias - 1)
    if inicio > fim:
        sys.exit("A data inicial é depois da final.")
    ate = fim + timedelta(days=1)  # data final exclusiva nas consultas

    print(f"Período: {inicio:%d/%m/%Y} a {fim:%d/%m/%Y}")
    with conectar(ler_env(RAIZ / ".env")) as cn:
        visitas = consultar(cn, "visitas.sql", inicio, ate)
        pedidos = consultar(cn, "pedidos.sql", inicio, ate)
        clientes = consultar(cn, "clientes.sql", inicio, ate, inicio, ate)
        vendedores = consultar(cn, "vendedores.sql")
        motivos = consultar(cn, "motivos.sql")
        rota = consultar(cn, "rota.sql")
        ciclos = consultar(cn, "ciclos.sql", inicio, ate)
    print(f"Lidos: {len(visitas)} visitas, {len(pedidos)} pedidos, {len(clientes)} clientes")

    dados = montar(visitas, pedidos, clientes, vendedores, motivos)

    # Rota prevista de cada vendedor no dia (cadastro de rota do sistema de pedidos, a mesma do GPS)
    from relatorios import SUPERVISORES_FORA, previstos_do_dia  # import aqui: relatorios.py também importa este arquivo
    previstos = {}

    # Vendedor com rota no dia mas sem nenhuma visita nem pedido também ganha uma linha (com zeros):
    # é justamente quem mais precisa aparecer. Supervisor 99 (Broker) fica de fora, como nos relatórios.
    cad = {e["vendedor"]: e for e in vendedores}
    tem = {(r["d"], r["v"]) for r in dados["registros"]}
    zeros = {"vis": 0, "cli": 0, "pos": 0, "posv": 0, "semv": 0, "nvp": 0, "rel": 0, "longe": 0, "semgps": 0,
             "ini": "", "fim": "", "jor": 0, "tcli": 0, "ped": 0, "val": 0, "mot": {}}
    dia = inicio
    while dia <= fim:
        previstos[dia] = previstos_do_dia(rota, ciclos, dia)
        for cod, clientes_rota in previstos[dia].items():
            e = cad.get((cod))
            if not clientes_rota or e is None or e["cod_supervisor"] in SUPERVISORES_FORA or e["cod_supervisor"] is None:
                continue
            if (dia.isoformat(), cod) not in tem:
                dados["registros"].append({"d": dia.isoformat(), "v": cod, "m": dict(zeros), "vis": [], "sv": []})
                dados["vendedores"].setdefault(cod, {"nome": (e["nome"] or "").strip(),
                                                     "sup": nome_sup(e, vendedores),
                                                     "cs": e["cod_supervisor"]})
        dia += timedelta(days=1)
    dados["registros"].sort(key=lambda r: (r["d"], r["v"]))

    for r in dados["registros"]:
        dia = date.fromisoformat(r["d"])
        if dia not in previstos:
            previstos[dia] = previstos_do_dia(rota, ciclos, dia)
        prev = previstos[dia].get(r["v"], set())
        visitados = {v[0] for v in r["vis"]}
        r["m"]["prev"] = len(prev)                    # clientes previstos na rota do dia
        r["m"]["pend"] = len(prev - visitados)        # previstos sem entrada
        r["m"]["fora"] = len(visitados - prev)        # visitados que não eram da rota do dia
        positivados = {v[0] for v in r["vis"] if v[7]} | {x[0] for x in r["sv"]}
        r["m"]["posr"] = len(positivados & prev)      # positivados que eram da rota do dia
        if dia >= fim - timedelta(days=DIAS_PREVISTOS_NO_MAPA - 1):
            r["pv"] = sorted(prev - visitados)        # clientes previstos sem entrada, para o mapa

    # Cadastro (nome, razão social, CNPJ e coordenada) dos previstos que ainda não estão em "clientes"
    faltam = sorted({pdv for r in dados["registros"] for pdv in r.get("pv", [])} - set(dados["clientes"]))
    if faltam:
        sql_base = (PASTA_SQL / "clientes_codigos.sql").read_text(encoding="utf-8")
        with conectar(ler_env(RAIZ / ".env")) as cn:
            cur = cn.cursor()
            for i in range(0, len(faltam), 1000):
                lote = faltam[i:i + 1000]
                cur.execute(sql_base.replace("{marcas}", ",".join("?" * len(lote))), *lote)
                for pdv, fant, razao, cnpj, lat, lon in cur.fetchall():
                    la, lo = coordenada(lat), coordenada(lon)
                    dados["clientes"][pdv] = [(fant or "").strip(), None if la is None else round(la, 5),
                                              None if lo is None else round(lo, 5), (razao or "").strip(), formata_cnpj(cnpj)]
    dados.update({
        "gerado": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "inicio": inicio.isoformat(),
        "fim": fim.isoformat(),
        "regras": {"relampago_seg": VISITA_RELAMPAGO_SEG, "longe_m": DISTANCIA_LONGE_M},
    })

    from relatorios import resumos_painel  # import aqui: relatorios.py também importa este arquivo
    dados["semanas"], dados["dias"] = resumos_painel(inicio, fim)
    print(f"Resumos: {len(dados['semanas'])} semana(s) completa(s) e {len(dados['dias'])} dia(s) no período")
    pot = PASTA_SAIDA / "potenciais.json"  # gerado pelo potenciais.py (clientes em potencial por vendedor)
    if pot.exists():
        dados["potenciais"] = json.loads(pot.read_text(encoding="utf-8"))
        print(f"Potenciais: {len(dados['potenciais']['pois'])} estabelecimentos (de {dados['potenciais']['gerado']})")

    PASTA_SAIDA.mkdir(exist_ok=True)
    html, csv_ = PASTA_SAIDA / "painel.html", PASTA_SAIDA / "vendedor_dia.csv"
    gravar_html(dados, html)
    gravar_csv(dados, csv_)
    print(f"{len(dados['registros'])} registros de vendedor por dia, {len(dados['vendedores'])} vendedores")
    print(f"Painel: {html}")
    print(f"CSV:    {csv_}")
    if args.abrir:
        os.startfile(html)  # noqa: S606 (Windows)


if __name__ == "__main__":
    main()
