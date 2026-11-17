"""Gera o painel de demonstração com dados 100% fictícios (nada vem de banco de dados).

Cria vendedores, supervisores, clientes, rotas, visitas e pedidos inventados, com semente fixa (os números são
sempre os mesmos), e passa tudo pelo mesmo motor de cálculo do painel real. Resultado: index.html na raiz.

    python gerar_demo.py            # últimos 31 dias úteis até o último dia útil
    python gerar_demo.py --dias 45
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
import tempfile
import types
from datetime import date, datetime, timedelta
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ / "motor"))
sys.modules.setdefault("pyodbc", types.SimpleNamespace(Connection=object, drivers=lambda: []))  # não usa banco

import gerar_painel as gp  # noqa: E402
import relatorios  # noqa: E402,F401

rnd = random.Random(2026)

# ---------------------------------------------------------------------------
# Cadastro inventado
# ---------------------------------------------------------------------------
CENTRO = (-25.4284, -49.2733)  # região de exemplo (mapa); os clientes abaixo são todos fictícios
SUPERVISORES = {  # código -> (nome, centro da área de atuação)
    10: ("Helena Duarte Ramos", (-25.4284, -49.2733)),
    20: ("Marcos Teixeira Lopes", (-25.4560, -49.2200)),
    30: ("Carla Menezes Barros", (-25.3900, -49.2900)),
    40: ("Rogério Albuquerque Lima", (-25.4700, -49.3100)),
}
NOMES = ["Ana Souza", "Bruno Carvalho", "Camila Freitas", "Diego Martins", "Elisa Nogueira", "Fábio Andrade",
         "Gabriela Rocha", "Heitor Pacheco", "Isabela Matos", "João Pedro Alves", "Karina Lopes", "Leandro Prado",
         "Mariana Costa", "Nelson Pires", "Olívia Ferraz", "Pedro Henrique Dias", "Quitéria Moraes", "Rafael Siqueira",
         "Sabrina Teles", "Thiago Barreto"]
TIPOS_LOJA = ["Mercadinho", "Padaria", "Lanchonete", "Restaurante", "Empório", "Mercearia", "Supermercado", "Cantina",
              "Pastelaria", "Casa de Sucos", "Minimercado", "Bar e Petiscos"]
SOBRENOMES = ["Bom Preço", "Dois Irmãos", "Sabor da Vila", "Esquina Doce", "Boa Mesa", "Central", "Vila Nova",
              "Estrela", "Sol Nascente", "Recanto", "Família Silva", "Ouro Verde", "Primavera", "Tio João",
              "Das Flores", "Do Bairro", "Aroma", "Girassol", "Pão Quente", "Mais Barato"]
MOTIVOS = {1: "Cliente sem dinheiro no momento", 2: "Estoque cheio", 3: "Estabelecimento fechado",
           4: "Comprou de outro fornecedor", 5: "Responsável ausente", 6: "Sem necessidade hoje"}
POR_SUP = 5      # vendedores por supervisor
CLIENTES_POR_VEND = 70


def metros_para_graus(m: float) -> float:
    return m / 111_320


def monta_cadastro():
    vendedores, clientes, rota = [], {}, []
    cod_cli = 1000
    nomes = NOMES[:]
    rnd.shuffle(nomes)
    for cs, (nome_sup, (clat, clon)) in SUPERVISORES.items():
        for i in range(POR_SUP):
            cod = cs * 10 + 1 + i  # 101.., 201.., ...
            vendedores.append({"vendedor": cod, "nome": nomes.pop().upper(), "cod_supervisor": cs,
                               "supervisor": nome_sup.upper()})
            # cada vendedor trabalha num bairro (um "miolo" perto do centro do supervisor)
            blat = clat + rnd.uniform(-0.018, 0.018)
            blon = clon + rnd.uniform(-0.018, 0.018)
            for k in range(CLIENTES_POR_VEND):
                cod_cli += 1
                ang, dist = rnd.uniform(0, 2 * math.pi), abs(rnd.gauss(0, 1100))
                lat = blat + metros_para_graus(dist * math.sin(ang))
                lon = blon + metros_para_graus(dist * math.cos(ang)) / math.cos(math.radians(blat))
                loja, nome = rnd.choice(TIPOS_LOJA), rnd.choice(SOBRENOMES)
                cnpj = "".join(str(rnd.randint(0, 9)) for _ in range(8)) + "0001" + "00"  # dígitos verificadores inválidos
                clientes[cod_cli] = {"pdv": cod_cli, "fantasia": f"{loja} {nome}"[:10], "razao": f"{nome} {loja} Ltda",
                                     "cnpj": cnpj, "lat": round(lat, 6), "lon": round(lon, 6), "vend": cod}
                ciclo = rnd.choices(["1234", "1 3 ", " 2 4"], weights=[70, 15, 15])[0]
                rota.append({"vendedor": cod, "pdv": cod_cli, "dia": rnd.randint(1, 5), "ciclo": ciclo})
    return vendedores, clientes, rota


# ---------------------------------------------------------------------------
# Movimento inventado (visitas e pedidos)
# ---------------------------------------------------------------------------
def ciclo_da_semana(d: date) -> int:
    return d.isocalendar()[1] % 4 + 1


def previstos(rota, d):
    ciclo = ciclo_da_semana(d)
    out = {}
    for r in rota:
        if r["dia"] == d.isoweekday() and str(ciclo) in r["ciclo"]:
            out.setdefault(r["vendedor"], []).append(r["pdv"])
    return out


def simula(vendedores, clientes, rota, inicio: date, fim: date):
    visitas, pedidos = [], []
    n_ped = 500000
    # perfis: a maioria vai bem; alguns têm dias problemáticos, para o painel ter o que mostrar
    perfil = {v["vendedor"]: rnd.choices(["bom", "medio", "fraco"], weights=[50, 35, 15])[0] for v in vendedores}
    d = inicio
    while d <= fim:
        if d.weekday() < 5:
            prev = previstos(rota, d)
            for v in vendedores:
                cod = v["vendedor"]
                p = perfil[cod]
                lista = prev.get(cod, [])[:]
                rnd.shuffle(lista)
                if not lista:
                    continue
                dia_ruim = rnd.random() < {"bom": 0.04, "medio": 0.12, "fraco": 0.3}[p]
                taxa_visita = {"bom": 0.93, "medio": 0.8, "fraco": 0.6}[p] * (0.5 if dia_ruim else 1)
                taxa_compra = {"bom": 0.62, "medio": 0.5, "fraco": 0.38}[p]
                sem_gps_dia = dia_ruim and rnd.random() < 0.4  # pedido sem nenhuma visita (aparelho sem sincronizar)
                alvo = [c for c in lista if rnd.random() < taxa_visita]
                # alguns clientes fora da rota
                extras = [c["pdv"] for c in rnd.sample(list(clientes.values()), 0)]
                if rnd.random() < 0.25:
                    extras = rnd.sample([c["pdv"] for c in clientes.values() if c["vend"] == cod and c["pdv"] not in lista], 2)
                alvo += extras
                hora = datetime(d.year, d.month, d.day, rnd.choice([7, 8, 8, 9]), rnd.randint(0, 50))
                fim_jornada = 12 if (dia_ruim and p != "bom") else 18
                seq = 0
                for pdv in alvo:
                    if hora.hour >= fim_jornada:
                        break
                    cli = clientes[pdv]
                    dur = rnd.choice([1, 1, 4, 6, 8, 10, 12, 15, 20, 28]) * 60 + rnd.randint(0, 50)
                    ent, sai = hora, hora + timedelta(seconds=dur)
                    longe = rnd.random() < 0.05
                    desloc = rnd.uniform(400, 1500) if longe else abs(rnd.gauss(0, 60))
                    ang = rnd.uniform(0, 2 * math.pi)
                    lat = cli["lat"] + metros_para_graus(desloc * math.sin(ang))
                    lon = cli["lon"] + metros_para_graus(desloc * math.cos(ang)) / math.cos(math.radians(cli["lat"]))
                    comprou = rnd.random() < taxa_compra and not sem_gps_dia
                    seq += 1
                    tipo_mot, cod_mot, pocket = None, None, None
                    if comprou:
                        n_ped += 1
                        pocket = n_ped
                        pedidos.append({"data": d, "vendedor": cod, "pdv": pdv, "pedido": n_ped, "pocket": pocket,
                                        "valor": round(rnd.lognormvariate(6.3, 0.6), 2),
                                        "origem": rnd.choices([None, 14], weights=[80, 20])[0]})
                    else:
                        tipo_mot, cod_mot = 2, rnd.choice(list(MOTIVOS))
                    visitas.append({"data": d, "vendedor": cod, "pdv": pdv, "seq": seq, "entrada": ent, "saida": sai,
                                    "tipo_motivo": tipo_mot, "cod_motivo": cod_mot, "pedido_pocket": pocket,
                                    "lat": f"{lat:.6f}", "lon": f"{lon:.6f}"})
                    hora = sai + timedelta(minutes=rnd.randint(6, 25))
                # pedidos de clientes que não foram visitados (pedido sem visita)
                for pdv in lista:
                    if pdv not in alvo and rnd.random() < (0.5 if sem_gps_dia else 0.1):
                        n_ped += 1
                        pedidos.append({"data": d, "vendedor": cod, "pdv": pdv, "pedido": n_ped, "pocket": None,
                                        "valor": round(rnd.lognormvariate(6.2, 0.6), 2),
                                        "origem": rnd.choices([None, 14], weights=[50, 50])[0]})
        d += timedelta(days=1)
    return visitas, pedidos


# ---------------------------------------------------------------------------
# "Banco" de mentira: responde às mesmas consultas que o painel faz, com os dados inventados
# ---------------------------------------------------------------------------
class BancoFalso:
    def __init__(self, tabelas: dict):
        self.t = tabelas

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):  # usado só para completar o cadastro dos clientes previstos no mapa
        banco = self

        class Cur:
            def execute(self, sql, *cods):
                self.cods = cods

            def fetchall(self):
                return [(c, banco.t["cli"][c]["fantasia"], banco.t["cli"][c]["razao"], banco.t["cli"][c]["cnpj"],
                         banco.t["cli"][c]["lat"], banco.t["cli"][c]["lon"]) for c in self.cods if c in banco.t["cli"]]
        return Cur()


def instala_banco_falso(vendedores, clientes, rota, visitas, pedidos, inicio, fim):
    ciclos, d = [], inicio - timedelta(days=inicio.weekday())
    while d <= fim:
        ciclos.append({"ciclo": ciclo_da_semana(d), "inicio": d, "fim": d + timedelta(days=6)})
        d += timedelta(days=7)
    tabelas = {
        "visitas.sql": visitas, "pedidos.sql": [{"data": p["data"], "vendedor": p["vendedor"], "pdv": p["pdv"],
                                                  "pedido": p["pedido"], "valor": p["valor"],
                                                  "pedido_pocket": p["pocket"], "origem": p["origem"]} for p in pedidos],
        "clientes.sql": list(clientes.values()), "vendedores.sql": vendedores,
        "motivos.sql": [{"tipo_motivo": 2, "cod_motivo": c, "motivo": m} for c, m in MOTIVOS.items()],
        "rota.sql": rota, "ciclos.sql": ciclos,
        "ativos.sql": [{"vendedor": v["vendedor"]} for v in vendedores],
        "cli": clientes,
    }
    gp.ler_env = lambda caminho: {}
    gp.conectar = lambda env: BancoFalso(tabelas)
    gp.consultar = lambda cn, arquivo, *params: tabelas[arquivo]
    pasta = Path(tempfile.mkdtemp())
    (pasta / "clientes_codigos.sql").write_text("-- {marcas}", encoding="utf-8")
    gp.PASTA_SQL = pasta
    return tabelas


def potenciais_ficticios(vendedores, clientes):
    tipos = {"rest": "Restaurantes e lanchonetes", "pad": "Padarias", "mer": "Mercados e mercearias", "out": "Outros alimentícios"}
    nomes = {"rest": ["Restaurante Sabor Caseiro", "Lanchonete do Zé", "Cantina Bella", "Pastelaria Dourada", "Bar do Largo"],
             "pad": ["Padaria Pão Quente", "Padaria Santa Clara", "Confeitaria Doce Vida"],
             "mer": ["Mercado Novo Dia", "Minimercado Central", "Supermercado Bom Lugar"],
             "out": ["Açougue Boi Gordo", "Hortifruti Verde Vivo", "Sorveteria Gelato"]}
    pois, vend = [], {}
    for v in vendedores:
        meus = [c for c in clientes.values() if c["vend"] == v["vendedor"]]
        ids = []
        for _ in range(rnd.randint(25, 45)):
            ref = rnd.choice(meus)
            t = rnd.choices(list(tipos), weights=[45, 20, 25, 10])[0]
            pois.append({"t": t, "n": rnd.choice(nomes[t]) if rnd.random() < 0.8 else None,
                         "lat": round(ref["lat"] + rnd.uniform(-0.004, 0.004), 6),
                         "lon": round(ref["lon"] + rnd.uniform(-0.004, 0.004), 6)})
            ids.append(len(pois) - 1)
        vend[str(v["vendedor"])] = ids
    return {"gerado": "dados fictícios", "tipos": tipos, "pois": pois, "vend": vend}


def main():
    ap = argparse.ArgumentParser(description="Gera o painel de demonstração (dados fictícios).")
    ap.add_argument("--dias", type=int, default=31)
    args = ap.parse_args()

    fim = date.today() - timedelta(days=1)
    while fim.weekday() > 4:  # último dia útil fechado
        fim -= timedelta(days=1)
    inicio = fim - timedelta(days=args.dias - 1)

    vendedores, clientes, rota = monta_cadastro()
    visitas, pedidos = simula(vendedores, clientes, rota, inicio, fim)
    instala_banco_falso(vendedores, clientes, rota, visitas, pedidos, inicio, fim)
    print(f"Fictício: {len(vendedores)} vendedores, {len(clientes)} clientes, {len(visitas)} visitas, {len(pedidos)} pedidos")

    # o motor grava saida/painel.html; a demo move para index.html
    saida = RAIZ / "saida"
    gp.PASTA_SAIDA = saida
    saida.mkdir(exist_ok=True)
    (saida / "potenciais.json").write_text(json.dumps(potenciais_ficticios(vendedores, clientes), ensure_ascii=False,
                                                      separators=(",", ":")), encoding="utf-8")
    sys.argv = ["gerar_painel.py", "--inicio", inicio.isoformat(), "--fim", fim.isoformat()]
    gp.main()
    (RAIZ / "index.html").write_text((saida / "painel.html").read_text(encoding="utf-8"), encoding="utf-8")
    print("Pronto: index.html")


if __name__ == "__main__":
    main()
