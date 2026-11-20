#!/usr/bin/env python3
"""FPL bytteassistent. Kun standardbibliotek (urllib). Lager rapport som .md og .html.

Bruk:
  python3 fpl_bytter.py                      # hent live data fra FPL-API
  python3 fpl_bytter.py --only-if-new-gw     # kjor bare hvis en ny runde er ferdigspilt
  python3 fpl_bytter.py --from-dir DIR       # bruk lagrede JSON-filer (test)
  python3 fpl_bytter.py --save-dir DIR       # lagre rad-data fra API (feilsoking)
"""
import argparse, datetime as dt, html, json, math, os, subprocess, sys, time, unicodedata
import urllib.request, urllib.error

BASE = "https://fantasy.premierleague.com/api/"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
POS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
GOAL_PTS = {1: 10, 2: 6, 3: 5, 4: 4}
CS_PTS = {1: 4, 2: 4, 3: 1, 4: 0}
WEIGHTS = [1.0, 0.95, 0.9, 0.85, 0.8, 0.75, 0.7]
SHRINK_K = 3.0  # "pseudo-90-minutter" mot posisjonssnitt

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = HERE if os.path.exists(os.path.join(HERE, "config.json")) else os.path.dirname(HERE)


# ---------- hjelpere ----------
def num(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def f1(x, nd=1):
    return f"{x:.{nd}f}".replace(".", ",")


def curl_get(url, data=None, headers=None):
    """Reserve: Macens egen curl bruker systemets sertifikater (løser SSL-feil i Python)."""
    cmd = ["/usr/bin/curl", "-sS", "--fail", "-m", "40", "-A", UA["User-Agent"]]
    for k, v in (headers or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    if data is not None:
        cmd += ["--data-binary", "@-"]
    cmd.append(url)
    r = subprocess.run(cmd, input=data, capture_output=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "ignore").strip() or f"curl feilet ({r.returncode})")
    return r.stdout


def http_get(path):
    last = None
    for i in range(3):
        try:
            req = urllib.request.Request(BASE + path, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            try:
                return json.loads(curl_get(BASE + path).decode("utf-8"))
            except Exception as e2:
                last = f"{e} / curl: {e2}"
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"Kunne ikke hente {path}: {last}")


class Source:
    def __init__(self, from_dir=None, save_dir=None):
        self.from_dir, self.save_dir = from_dir, save_dir
        if save_dir:
            os.makedirs(save_dir, exist_ok=True)

    def get(self, name, path, optional=False):
        if self.from_dir:
            p = os.path.join(self.from_dir, name + ".json")
            if not os.path.exists(p):
                if optional:
                    return None
                raise FileNotFoundError(p)
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        try:
            data = http_get(path)
        except RuntimeError:
            if optional:
                return None
            raise
        if self.save_dir:
            with open(os.path.join(self.save_dir, name + ".json"), "w", encoding="utf-8") as f:
                json.dump(data, f)
        return data


# ---------- modell ----------
class Model:
    def __init__(self, boot, fixtures, cfg):
        self.cfg = cfg
        self.teams = {t["id"]: t for t in boot["teams"]}
        self.events = boot["events"]
        finished = [e for e in self.events if e.get("finished")]
        self.played = max(1, len(finished))
        self.last_finished = max([e["id"] for e in finished], default=0)
        nxt = next((e for e in self.events if e.get("is_next")), None)
        if nxt is None:
            raise SystemExit("Ingen kommende runde funnet (sesongen er over?).")
        self.next_event = nxt
        self.target = nxt["id"]
        horizon = int(cfg.get("horizon", 5))
        self.gws = [g for g in range(self.target, min(38, self.target + horizon - 1) + 1)]
        self.w = WEIGHTS[: len(self.gws)]
        self._fix(fixtures)
        self._strength()
        self._players(boot["elements"])

    # fixtures per (lag, gw)
    def _fix(self, fixtures):
        self.fx = {}
        for f in fixtures:
            ev = f.get("event")
            if ev is None or f.get("finished"):
                continue
            self.fx.setdefault((f["team_h"], ev), []).append(
                {"opp": f["team_a"], "home": True, "diff": f.get("team_h_difficulty", 3)})
            self.fx.setdefault((f["team_a"], ev), []).append(
                {"opp": f["team_h"], "home": False, "diff": f.get("team_a_difficulty", 3)})

    def sv(self, t, kind, side):
        """Lagstyrke (kind: attack/defence, side: home/away) med reserve hvis feltet mangler eller er 0."""
        for key in (f"strength_{kind}_{side}", f"strength_overall_{side}", "strength"):
            v = num(t.get(key))
            if v > 0:
                # "strength" (1-5) er på en annen skala: gjør om til ca. 1000-1400
                return v if v > 20 else 900 + 100 * v
        return 0.0

    def _strength(self):
        T = list(self.teams.values())

        def mean(kind):
            vals = [self.sv(t, kind, side) for t in T for side in ("home", "away")]
            vals = [v for v in vals if v > 0]
            return sum(vals) / len(vals) if vals else 0.0

        self.avg_def, self.avg_att = mean("defence"), mean("attack")

    def mults(self, f):
        o = self.teams[f["opp"]]
        side = "away" if f["home"] else "home"
        od, oa = self.sv(o, "defence", side), self.sv(o, "attack", side)
        ha = 1.04 if f["home"] else 0.96
        att = clamp((self.avg_def / od) ** 1.5 * ha, 0.6, 1.6) if od > 0 and self.avg_def > 0 else ha
        cs = clamp((self.avg_att / oa) ** 1.5 * ha, 0.6, 1.6) if oa > 0 and self.avg_att > 0 else ha
        return att, cs

    def _players(self, elements):
        # posisjonssnitt per 90 til shrinkage
        agg = {p: dict(n90=0.0, xg=0.0, xa=0.0, xgc=0.0, sv=0.0, bo=0.0) for p in POS}
        for e in elements:
            if e["minutes"] > 0:
                a, n = agg[e["element_type"]], e["minutes"] / 90
                a["n90"] += n
                a["xg"] += num(e.get("expected_goals"))
                a["xa"] += num(e.get("expected_assists"))
                a["xgc"] += num(e.get("expected_goals_conceded"))
                a["sv"] += num(e.get("saves"))
                a["bo"] += num(e.get("bonus"))
        avg = {p: {k: (v / a["n90"] if a["n90"] else 0.0) for k, v in a.items() if k != "n90"}
               for p, a in agg.items()}

        def shr(pos, key, tot, n90):
            return (tot + avg[pos][key] * SHRINK_K) / (n90 + SHRINK_K)

        self.players = {}
        for e in elements:
            pos, mins = e["element_type"], e["minutes"]
            n90 = mins / 90
            pr = dict(
                xg=shr(pos, "xg", num(e.get("expected_goals")), n90),
                xa=shr(pos, "xa", num(e.get("expected_assists")), n90),
                xgc=shr(pos, "xgc", num(e.get("expected_goals_conceded")), n90),
                sv=shr(pos, "sv", num(e.get("saves")), n90),
                bo=shr(pos, "bo", num(e.get("bonus")), n90), pos=pos)
            share = min(1.0, mins / (90 * self.played))
            form = num(e.get("form"))
            starts = e.get("starts") or 0
            if starts >= 3 and form >= 3.0 and e.get("status") == "a":
                share = max(share, min(1.0, mins / max(starts, 1) / 90) * 0.8)
            status = e.get("status", "a")
            cpn = e.get("chance_of_playing_next_round")
            if cpn is not None:
                a = cpn / 100
            else:
                a = {"a": 1.0, "d": 0.75}.get(status, 0.0)
            p = dict(id=e["id"], name=e["web_name"], team=e["team"], pos=pos,
                     price=e["now_cost"] / 10, own=num(e.get("selected_by_percent")),
                     form=form, total=e["total_points"], minutes=mins, status=status,
                     avail=a, share=share, pr=pr, news=e.get("news", ""))
            neutral = self._game(p, 1.0, 1.0)
            p["neutral"] = neutral
            base = neutral
            if share > 0.3:
                base = 0.7 * neutral + 0.3 * form
            p["base"] = base
            # sesongens forventede poeng (til over/underprestering)
            noapp = pts90(pr, 1.0, 1.0)
            p["season_x"] = n90 * (noapp + 2.0)
            p["luck"] = e["total_points"] - p["season_x"]
            self._project(p)
            self.players[p["id"]] = p

    def _game(self, p, am, cm):
        return p["share"] * pts90(p["pr"], am, cm) + 2 * min(1.0, p["share"] * 1.15)

    def _project(self, p):
        eps, fx_txt, fxl = [], [], []
        for i, g in enumerate(self.gws):
            fl = self.fx.get((p["team"], g), [])
            a = p["avail"]
            if i > 0 and p["status"] not in ("s", "u", "n"):
                a = a + (1 - a) * 0.5
            tot = 0.0
            for f in fl:
                am, cm = self.mults(f)
                mf = self._game(p, am, cm)
                ratio = mf / p["neutral"] if p["neutral"] > 0.05 else 1.0
                tot += p["base"] * ratio * a
                tok = f"{self.teams[f['opp']]['short_name']}({'H' if f['home'] else 'B'})"
                fx_txt.append(tok)
                fxl.append((tok, (am + cm) / 2))
            if not fl:
                fx_txt.append("-")
                fxl.append(("-", 1.0))
            eps.append(tot)
        p["eps"] = eps
        p["ep1"] = eps[0] if eps else 0.0
        p["ep5"] = sum(w * x for w, x in zip(self.w, eps))
        p["fx"] = " ".join(fx_txt[:6])
        p["fxl"] = fxl[:6]
        p["val"] = p["ep5"] / p["price"] if p["price"] else 0.0

    def tname(self, tid):
        return self.teams[tid]["short_name"]


def pts90(pr, am, cm):
    pos = pr["pos"]
    att = (pr["xg"] * GOAL_PTS[pos] + pr["xa"] * 3) * am
    lam = max(0.05, pr["xgc"] / cm)
    cs = math.exp(-lam) * CS_PTS[pos]
    gc = -(lam / 2 - (1 - math.exp(-2 * lam)) / 4) if pos in (1, 2) else 0.0
    sv = (pr["sv"] / 3) * (1 / cm) ** 0.5 if pos == 1 else 0.0
    return att + cs + gc + sv + pr["bo"]


# ---------- lag og bytter ----------
def load_squad(src, cfg, M):
    tid = cfg.get("team_id")
    if not tid:
        return None
    lab = cfg.get("name", "meg")
    gw = M.last_finished or 1
    picks = src.get(f"picks_{lab}", f"entry/{tid}/event/{gw}/picks/", optional=True)
    if not picks:
        print("Fikk ikke hentet laget (sjekk team_id).", file=sys.stderr)
        return None
    ids = [p["element"] for p in picks["picks"]]
    bank = picks.get("entry_history", {}).get("bank", 0)
    note = ""
    tr = src.get(f"transfers_{lab}", f"entry/{tid}/transfers/", optional=True) or []
    pend = [t for t in tr if t.get("event") == M.target]
    for t in pend:
        if t["element_out"] in ids:
            ids[ids.index(t["element_out"])] = t["element_in"]
            bank += t.get("element_out_cost", 0) - t.get("element_in_cost", 0)
    if pend:
        note = f"Inkluderer {len(pend)} allerede gjorte bytte(r) for GW{M.target}."
    ids = [i for i in ids if i in M.players]
    manual = cfg.get("free_transfers")
    if isinstance(manual, int):
        free = manual
    else:
        free = estimate_free(src.get(f"history_{lab}", f"entry/{tid}/history/", optional=True), M)
        free = max(0, free - len(pend)) if free > len(pend) else (1 if not pend else 0)
        if pend:
            free = max(0, estimate_free(src.get(f"history_{lab}", f"entry/{tid}/history/", optional=True), M) - len(pend))
        note = (note + " " if note else "") + "Gratisbytter er estimert fra historikken."
    hist = src.get(f"history_{lab}", f"entry/{tid}/history/", optional=True) or {}
    chips_used = [f"{c['name']} (GW{c['event']})" for c in hist.get("chips", [])]
    val = picks.get("entry_history", {}).get("value", 0) / 10 if not pend else 0
    return dict(ids=ids, bank=bank / 10, note=note, gw=gw, free=free, value=val, chips_used=chips_used)


def estimate_free(hist, M):
    """Estimerer gratisbytter foran neste runde (maks 5 kan spares)."""
    if not hist:
        return 1
    chips = {c["event"]: c["name"] for c in hist.get("chips", [])}
    ft = 1
    for r in sorted(hist.get("current", []), key=lambda x: x["event"]):
        ev = r["event"]
        if ev <= 1 or ev > M.last_finished:
            continue
        used = r.get("event_transfers", 0)
        if chips.get(ev) in ("wildcard", "freehit"):
            ft = ft
        else:
            ft = min(5, max(ft - used, 0) + 1)
    return ft


def find_transfers(M, ids, bank, used=(), n_per_out=2):
    squad = set(ids)
    cnt = {}
    for i in ids:
        cnt[M.players[i]["team"]] = cnt.get(M.players[i]["team"], 0) + 1
    out = []
    for o in ids:
        if o in used:
            continue
        po = M.players[o]
        cands = []
        for q in M.players.values():
            if q["pos"] != po["pos"] or q["id"] in squad or q["id"] in used:
                continue
            if q["avail"] < 0.75 or q["share"] < 0.35:
                continue
            if q["price"] > po["price"] + bank + 1e-9:
                continue
            c = cnt.get(q["team"], 0) - (1 if q["team"] == po["team"] else 0)
            if c >= 3:
                continue
            cands.append((q["ep5"] - po["ep5"], q))
        cands.sort(key=lambda x: -x[0])
        for gain, q in cands[:n_per_out]:
            out.append(dict(out=po, inn=q, gain=gain, gain1=q["ep1"] - po["ep1"],
                            bank=bank + po["price"] - q["price"]))
    out.sort(key=lambda x: -x["gain"])
    return out


def two_step(M, ids, bank, free):
    first = find_transfers(M, ids, bank, n_per_out=1)
    if not first:
        return None
    best = None
    for t1 in first[:6]:
        ids2 = [t1["inn"]["id"] if i == t1["out"]["id"] else i for i in ids]
        second = find_transfers(M, ids2, t1["bank"], used={t1["inn"]["id"]}, n_per_out=1)
        if not second:
            continue
        t2 = second[0]
        net = t1["gain"] + t2["gain"] - 4 * max(0, 2 - free)
        if best is None or net > best[0]:
            best = (net, t1, t2)
    return best


# ---------- rapport ----------
WD = ["man", "tir", "ons", "tor", "fre", "lør", "søn"]
POSCLS = {1: "gk", 2: "def", 3: "mid", 4: "fwd"}
esc = html.escape


class H(str):
    """Tekst som brukes i .md, med valgfri HTML-variant (.html) til nettsiden."""
    def __new__(cls, text, html_=None):
        o = super().__new__(cls, text)
        o.html = html_
        return o


def pos_cell(pos):
    return H(POS[pos], f'<span class="pos {POSCLS[pos]}">{POS[pos]}</span>')


def name_cell(p):
    return H(p["name"], f'<b>{esc(p["name"])}</b>')


def fx_cell(p):
    chips = []
    for txt, ease in p["fxl"]:
        cls = "ez" if ease >= 1.12 else "hd" if ease <= 0.9 else "md"
        chips.append(f'<span class="fx {cls}">{esc(txt)}</span>')
    return H(p["fx"], "".join(chips))


def gain_cell(x):
    t = f"{x:+.1f}".replace(".", ",")
    cls = "up" if x > 0.05 else "dn" if x < -0.05 else "fl"
    return H(t, f'<span class="g {cls}">{t}</span>')


def bar_cell(v, vmax):
    t = f1(v)
    w = max(4, min(100, v / vmax * 100)) if vmax else 0
    return H(t, f'<span class="bv">{t}</span><span class="bar"><i style="width:{w:.0f}%"></i></span>')


def flags_cell(flags):
    if not flags:
        return H("-", '<span class="muted">–</span>')
    return H(", ".join(flags), "".join(f'<span class="flag">{esc(f)}</span>' for f in flags))


def table(headers, rows):
    return ("table", headers, rows)


def prow(M, p):
    return [name_cell(p), pos_cell(p["pos"]), M.tname(p["team"]), f1(p["price"]), f1(p["ep1"]), f1(p["ep5"]),
            f1(p["val"], 2), f1(p["own"]) + "%", f1(p["form"]), fx_cell(p)]


PH = ["Spiller", "Pos", "Lag", "£", "EP neste", "EP 5 r.", "EP/£", "Eid", "Form", "Neste kamper"]


def build_report(M, squad, cfg):
    B = []
    dl = M.next_event.get("deadline_time")
    try:
        d = dt.datetime.fromisoformat(dl.replace("Z", "+00:00")).astimezone()
        dls = f"{WD[d.weekday()]} {d:%d.%m.} kl. {d:%H:%M}"
    except Exception:
        dls = str(dl)
    who = f" – {cfg['name']}" if cfg.get("name") else ""
    B.append(("hero", f"FPL-anbefaling{who}",
              f"GW{M.target} · frist {dls} · horisont GW{M.gws[0]}–GW{M.gws[-1]} · oppdatert {dt.datetime.now():%d.%m.%Y %H:%M}"))
    P = [p for p in M.players.values() if p["minutes"] > 0]
    summary = []
    kpis = []

    if squad:
        ids, bank = squad["ids"], squad["bank"]
        free = squad.get("free", int(cfg.get("free_transfers", 1)))
        S = [M.players[i] for i in ids]
        singles = find_transfers(M, ids, bank)
        caps = sorted(S, key=lambda p: -(0.7 * p["ep1"] + 0.3 * p["eps"][1] if len(p["eps"]) > 1 else p["ep1"]))[:3]
        if singles:
            t0 = singles[0]
            kpis.append(("Beste bytte", f"{t0['out']['name']} → {t0['inn']['name']}",
                         f"{t0['gain']:+.1f} EP over {len(M.gws)} runder".replace(".", ",")))
        kpis.append(("Kaptein", caps[0]["name"], f"{f1(caps[0]['ep1'] * 2)} EP (x2) · {caps[0]['fx'].split(' ')[0]}"))
        kpis.append(("Bank", f"£{f1(bank)}m", "salgspris = dagens pris"))
        kpis.append(("Gratisbytter", str(free), "estimert fra historikk"))
        B.append(("kpis", kpis))

        B.append(("h2", "Anbefalte bytter"))
        if squad["note"] and "allerede" in squad["note"]:
            B.append(("p", squad["note"].replace(" Gratisbytter er estimert fra historikken.", "")))
        if singles:
            rows, items = [], []
            for t in singles[:8]:
                o, i = t["out"], t["inn"]
                rows.append([f"{o['name']} → {i['name']}", pos_cell(o["pos"]), f"{f1(o['price'])} → {f1(i['price'])}",
                             gain_cell(t["gain1"]), gain_cell(t["gain"]), fx_cell(i)])
                items.append(dict(o=o["name"], i=i["name"], pos=o["pos"], po=f1(o["price"]), pi=f1(i["price"]),
                                  g1=gain_cell(t["gain1"]), g5=gain_cell(t["gain"]), fx=fx_cell(i),
                                  n=len(M.gws)))
            B.append(("transfers", ["Bytte", "Pos", "Pris", "Gevinst neste", f"Gevinst {len(M.gws)} r.", "Nye spillers kamper"], rows, items))
            for t3 in singles[:3]:
                summary.append(f"Bytte: {t3['out']['name']} → {t3['inn']['name']} ({t3['gain']:+.1f} EP/{len(M.gws)} r.)")
            ts = two_step(M, ids, bank, free)
            if ts:
                net, t1, t2 = ts
                hit = " (inkl. -4 for ekstra bytte)" if free < 2 else ""
                B.append(("callout", f"Beste to bytter samlet: {t1['out']['name']} → {t1['inn']['name']} og "
                                     f"{t2['out']['name']} → {t2['inn']['name']}. Netto {f1(net)} EP{hit}."))
        else:
            B.append(("p", "Fant ingen bytter som øker forventede poeng innenfor budsjettet."))

        B.append(("h2", "Svakeste ledd i laget"))
        weak = sorted(S, key=lambda p: p["ep5"])[:6]
        rows = []
        for p in weak:
            flag = []
            if p["share"] < 0.35:
                flag.append("reserve")
            if p["avail"] < 1:
                flag.append("tvil/skade")
            if p["luck"] > 12:
                flag.append("overpresterer")
            r = prow(M, p)
            rows.append(r[:9] + [flags_cell(flag), r[9]])
        B.append(table(PH[:9] + ["Flagg", "Neste kamper"], rows))

        B.append(("h2", "Kaptein"))
        B.append(table(["Spiller", "EP neste (x2)", "Neste kamper"],
                       [[name_cell(p), f1(p["ep1"] * 2), fx_cell(p)] for p in caps]))
        summary.append(f"Kaptein: {caps[0]['name']}")

        B.append(("h2", "Chip-vinduer"))
        rows, hints, anyev = [], [], False
        for g in range(M.target, min(38, M.target + 7) + 1):
            dbl = sorted({M.tname(t) for (t, e), fl in M.fx.items() if e == g and len(fl) >= 2})
            blk_teams = {t for t in M.teams if not M.fx.get((t, g))}
            sq_d = sum(1 for p in S if len(M.fx.get((p["team"], g), [])) >= 2)
            sq_b = sum(1 for p in S if p["team"] in blk_teams)
            if sq_d or sq_b or dbl:
                anyev = True
            rows.append([f"GW{g}", str(sq_d), str(sq_b), ", ".join(dbl) or "-"])
            if sq_d >= 5:
                hints.append(f"GW{g}: {sq_d} spillere med dobbeltrunde → vurder Bench Boost/Triple Captain.")
            if sq_b >= 4:
                hints.append(f"GW{g}: {sq_b} spillere uten kamp → vurder Free Hit.")
        if anyev:
            B.append(table(["Runde", "Dobbel (dine)", "Blank (dine)", "Lag med dobbeltrunde"], rows))
            for h in hints:
                B.append(("p", h))
        else:
            B.append(("p", "Ingen dobbelt- eller blankrunder de neste 8 rundene, så ingen tydelige chip-vinduer akkurat nå."))
        B.extend(chip_sections(M, squad, S))
    else:
        B.append(("p", "Ingen team_id i config.json: lagspesifikke deler er hoppet over."))

    B.append(("h2", "Mest verdi per posisjon"))
    ok = [p for p in M.players.values() if p["avail"] >= 0.75 and p["share"] >= 0.5]
    for pos in (4, 3, 2, 1):
        top = sorted([p for p in ok if p["pos"] == pos], key=lambda p: -p["val"])[:5]
        B.append(("h3", {4: "Spisser", 3: "Midtbane", 2: "Forsvar", 1: "Keeper"}[pos]))
        B.append(table(PH, [prow(M, p) for p in top]))

    B.append(("h2", "Differensialer (eid under 10 %)"))
    diff = sorted([p for p in ok if p["own"] < 10], key=lambda p: -p["ep5"])[:8]
    B.append(table(PH, [prow(M, p) for p in diff]))

    B.append(("h2", "Over- og underpresterere"))
    reg = [p for p in P if p["minutes"] >= 450]
    over = sorted(reg, key=lambda p: -p["luck"])[:6]
    under = sorted(reg, key=lambda p: p["luck"])[:6]
    B.append(("p", "Poeng så langt minus det xG/xA/xGC tilsier. Høy verdi = har hatt flaks (selg-risiko), lav = underpresterer (kjøpskandidat)."))
    hd = ["Spiller", "Pos", "Lag", "£", "Poeng", "Forventet", "Differanse"]

    def lrow(p):
        return [name_cell(p), pos_cell(p["pos"]), M.tname(p["team"]), f1(p["price"]), str(p["total"]),
                f1(p["season_x"]), gain_cell(p["luck"])]
    B.append(("h3", "Overpresterer (selg-risiko)"))
    B.append(table(hd, [lrow(p) for p in over]))
    B.append(("h3", "Underpresterer (kjøp-kandidat)"))
    B.append(table(hd, [lrow(p) for p in under]))

    B.append(("h2", "Hva har lønnet seg i sesongen"))
    rows, vals = [], []
    for pos in (4, 3, 2, 1):
        xs = sorted([p for p in reg if p["pos"] == pos], key=lambda p: -p["total"])[:15]
        if not xs:
            continue
        pr = sum(p["price"] for p in xs) / len(xs)
        pt = sum(p["total"] for p in xs) / len(xs)
        bv = max((p for p in reg if p["pos"] == pos), key=lambda p: p["total"] / p["price"])
        vals.append((pos, pr, pt, pt / pr, bv))
    vmax = max([v[3] for v in vals], default=1)
    for pos, pr, pt, ppm, bv in vals:
        rows.append([pos_cell(pos), f1(pr), f1(pt, 0), bar_cell(ppm, vmax), f"{bv['name']} ({f1(bv['total'] / bv['price'])})"])
    B.append(table(["Pos", "Snittpris topp 15", "Snittpoeng topp 15", "Poeng per £m", "Best poeng/£m"], rows))
    B.append(("p", "Høyt poeng/£m betyr at posisjonen gir mest igjen for pengene. Bruk det til å bestemme hvor du legger dyre spillere og hvor du sparer."))

    B.append(("details", "Slik leses tallene", [
        "EP = forventede poeng per kamp fra xG, xA, xGC (clean sheet, baklengsmål), redninger og bonus, "
        "justert for motstander (lagstyrke hjemme/borte) og sannsynlighet for å spille, blandet 70/30 med nåværende form.",
        "EP 5 r. er vektet sum for de neste kampene (nyere runder teller mer). Dobbeltrunder summeres, blanke runder gir 0.",
        "Kampfarge: grønn = lett motstander, rød = tung motstander. H = hjemme, B = borte.",
        "Modellen vet ikke om taktikk, skiftende straffetakere eller skader som ikke står i API-et. Sjekk pressekonferanser før frist.",
        "Spillere som har vært skadet tidligere i sesongen kan undervurderes pga. lav minuttandel.",
    ]))
    return B, summary


def to_md(B):
    out = []
    for b in B:
        k = b[0]
        if k == "hero":
            out.append(f"# {b[1]}\n\n{b[2]}\n")
        elif k == "kpis":
            out.extend(f"- **{l}:** {v} ({s})" for l, v, s in b[1])
            out.append("")
        elif k == "h2":
            out.append(f"\n## {b[1]}\n")
        elif k == "h3":
            out.append(f"\n### {b[1]}\n")
        elif k in ("p", "callout"):
            out.append(b[1] + "\n")
        elif k == "details":
            out.append(f"\n## {b[1]}\n")
            out.extend(t + "\n" for t in b[2])
        else:
            h, rows = b[1], b[2]
            out.append("| " + " | ".join(h) + " |")
            out.append("|" + "|".join("---" for _ in h) + "|")
            for r in rows:
                out.append("| " + " | ".join(str(c) for c in r) + " |")
            out.append("")
    return "\n".join(out)


CSS = """
:root{--bg:#f3f1f6;--card:#fff;--ink:#1d1730;--mut:#6b6580;--line:#e7e3ee;--brand:#37003c;--acc:#00c46a;--up:#0a8f4f;--dn:#c62840;--shadow:0 1px 2px rgba(30,10,50,.06),0 4px 14px rgba(30,10,50,.06)}
@media(prefers-color-scheme:dark){:root{--bg:#120d1c;--card:#1c1530;--ink:#f1eefa;--mut:#a59fba;--line:#322a4a;--up:#3ddc97;--dn:#ff6b81;--shadow:none}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:0 12px 40px}
.hero{background:linear-gradient(135deg,#37003c 0%,#5b1170 55%,#00a65a 130%);color:#fff;padding:26px 16px 22px;margin:0 -12px 14px}
.hero .in{max-width:980px;margin:0 auto}.hero h1{margin:0 0 4px;font-size:1.55em;letter-spacing:-.01em}
.hero p{margin:0;opacity:.85;font-size:.88em}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px;margin:-34px 0 14px}
.kpi{background:var(--card);border-radius:14px;padding:12px 14px;box-shadow:var(--shadow);border:1px solid var(--line)}
.kpi small{display:block;color:var(--mut);font-size:.72em;text-transform:uppercase;letter-spacing:.06em}
.kpi b{display:block;font-size:1.12em;margin:2px 0;overflow-wrap:anywhere}.kpi span{color:var(--mut);font-size:.8em}
section.card,details.card{background:var(--card);border-radius:16px;padding:14px 14px 12px;margin:0 0 14px;box-shadow:var(--shadow);border:1px solid var(--line)}
h2{font-size:1.1em;margin:0 0 10px;display:flex;align-items:center;gap:8px}
h2:before{content:"";width:5px;height:18px;border-radius:3px;background:var(--acc)}
h3{font-size:.8em;text-transform:uppercase;letter-spacing:.07em;color:var(--mut);margin:16px 0 6px}
p{margin:6px 0;color:var(--mut);font-size:.9em}
.callout{background:rgba(0,196,106,.1);border:1px solid rgba(0,196,106,.35);border-radius:12px;padding:10px 12px;margin:10px 0 2px;font-size:.92em}
.tr{border:1px solid var(--line);border-radius:12px;padding:10px 12px;margin:0 0 8px}
.tr-top{display:flex;align-items:center;gap:10px}.tr-names{flex:1;min-width:0;font-weight:600}
.tr-names .out{color:var(--dn)}.tr-names .in{color:var(--up)}.tr-names .arr{margin:0 6px;color:var(--mut)}
.tr-gain{text-align:right;line-height:1.1}.tr-gain small{display:block;color:var(--mut);font-size:.7em}.tr-gain .g{font-size:1.25em}
.tr-meta{display:flex;flex-wrap:wrap;gap:6px 12px;align-items:center;margin-top:6px;color:var(--mut);font-size:.82em}
.wrapt{overflow-x:auto;margin:0 -14px;padding:0 14px}
table{border-collapse:separate;border-spacing:0;width:100%;font-size:.84em}
th{position:sticky;top:0;text-align:left;font-weight:600;color:var(--mut);font-size:.78em;text-transform:uppercase;letter-spacing:.04em;padding:6px 8px;border-bottom:2px solid var(--line);white-space:nowrap;background:var(--card)}
td{padding:7px 8px;border-bottom:1px solid var(--line);white-space:nowrap;font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:0}td:first-child,th:first-child{position:sticky;left:0;background:var(--card);z-index:1}
.pos{display:inline-block;min-width:34px;text-align:center;border-radius:6px;padding:1px 6px;font-size:.78em;font-weight:700;color:#fff}
.pos.gk{background:#d99a00}.pos.def{background:#2f6fe4}.pos.mid{background:#12a05a}.pos.fwd{background:#d6334f}
.fx{display:inline-block;border-radius:6px;padding:1px 6px;margin:0 3px 0 0;font-size:.82em;font-weight:600}
.fx.ez{background:rgba(0,196,106,.18);color:var(--up)}.fx.md{background:rgba(120,110,150,.16);color:var(--ink)}.fx.hd{background:rgba(220,40,70,.16);color:var(--dn)}
.g{font-weight:700}.g.up{color:var(--up)}.g.dn{color:var(--dn)}.g.fl{color:var(--mut)}
.flag{display:inline-block;background:rgba(255,160,0,.18);color:#b36b00;border-radius:999px;padding:1px 8px;margin-right:4px;font-size:.8em;font-weight:600}
@media(prefers-color-scheme:dark){.flag{color:#ffc15a}}
.bv{display:inline-block;min-width:34px}.bar{display:inline-block;vertical-align:middle;width:90px;height:7px;border-radius:4px;background:var(--line);margin-left:6px}
.bar i{display:block;height:100%;border-radius:4px;background:var(--acc)}.muted{color:var(--mut)}
summary{cursor:pointer;font-weight:600}details p{margin:8px 0}
@media(max-width:520px){.hero h1{font-size:1.3em}.kpis{grid-template-columns:1fr 1fr}.tr-top{flex-wrap:wrap}}
"""


def cell_html(c):
    h = getattr(c, "html", None)
    return h if h else esc(str(c))


def to_html(B):
    h = ['<!doctype html><html lang="no"><head><meta charset="utf-8">',
         '<meta name="viewport" content="width=device-width,initial-scale=1">',
         '<meta name="color-scheme" content="light dark">',
         f"<title>FPL-anbefaling</title><style>{CSS}</style></head><body>"]
    open_sec = False

    def close():
        nonlocal open_sec
        if open_sec:
            h.append("</section>")
            open_sec = False

    h_hero_done = False
    for b in B:
        k = b[0]
        if k == "hero":
            h.append(f'<div class="hero"><div class="in"><h1>{esc(b[1])}</h1><p>{esc(b[2])}</p></div></div><div class="wrap">')
            h_hero_done = True
        elif k == "kpis":
            h.append('<div class="kpis">' + "".join(
                f'<div class="kpi"><small>{esc(l)}</small><b>{esc(v)}</b><span>{esc(s)}</span></div>' for l, v, s in b[1]) + "</div>")
        elif k == "h2":
            close()
            h.append(f'<section class="card"><h2>{esc(b[1])}</h2>')
            open_sec = True
        elif k == "h3":
            h.append(f"<h3>{esc(b[1])}</h3>")
        elif k == "p":
            h.append(f"<p>{esc(b[1])}</p>")
        elif k == "callout":
            h.append(f'<div class="callout">{esc(b[1])}</div>')
        elif k == "details":
            close()
            h.append(f'<details class="card"><summary>{esc(b[1])}</summary>' + "".join(f"<p>{esc(t)}</p>" for t in b[2]) + "</details>")
        elif k == "transfers":
            for it in b[3]:
                h.append(
                    '<div class="tr"><div class="tr-top">' + cell_html(pos_cell(it["pos"])) +
                    f'<div class="tr-names"><span class="out">{esc(it["o"])}</span><span class="arr">→</span><span class="in">{esc(it["i"])}</span></div>'
                    f'<div class="tr-gain">{cell_html(it["g5"])}<small>EP over {it["n"]} r.</small></div></div>'
                    f'<div class="tr-meta"><span>£{it["po"]} → £{it["pi"]}</span><span>Neste runde {cell_html(it["g1"])}</span>'
                    f'<span>{cell_html(it["fx"])}</span></div></div>')
        else:
            hd, rows = b[1], b[2]
            h.append('<div class="wrapt"><table><tr>' + "".join(f"<th>{esc(x)}</th>" for x in hd) + "</tr>")
            for r in rows:
                h.append("<tr>" + "".join(f"<td>{cell_html(c)}</td>" for c in r) + "</tr>")
            h.append("</table></div>")
    close()
    h.append("</div></body></html>")
    return "\n".join(h)


# ---------- Wildcard / Free Hit ----------
SQUAD_SHAPE = {1: 2, 2: 5, 3: 5, 4: 3}
XI_MAX = {1: 1, 2: 5, 3: 5, 4: 3}


def pick_xi(players, key):
    """Beste startellever (1 GK, min 3 DEF, 2 MID, 1 FWD) for en 15-manns tropp. Returnerer (poeng, xi, benk, kaptein)."""
    by = {pos: sorted([p for p in players if p["pos"] == pos], key=lambda p: -p[key]) for pos in POS}
    xi = [by[1][0]] + by[2][:3] + by[3][:2] + by[4][:1]
    cnt = {2: 3, 3: 2, 4: 1}
    rest = sorted(by[2][3:] + by[3][2:] + by[4][1:], key=lambda p: -p[key])
    for p in rest:
        if len(xi) == 11:
            break
        if cnt[p["pos"]] < XI_MAX[p["pos"]]:
            xi.append(p)
            cnt[p["pos"]] += 1
    ids = {id(p) for p in xi}
    bench = [p for p in players if id(p) not in ids]
    cap = max(xi, key=lambda p: p[key])
    score = sum(p[key] for p in xi) + cap[key] + 0.15 * sum(max(p[key], 0) for p in bench)
    return score, xi, bench, cap


def optimise_squad(M, budget, key, seeds, iters=20000):
    import random
    rnd = random.Random(7)
    pool_all = [p for p in M.players.values() if p["status"] not in ("u", "n")]
    pool = {}
    for pos in POS:
        ps = [p for p in pool_all if p["pos"] == pos]
        good = [p for p in ps if p["avail"] >= 0.75 and p["share"] >= 0.3]
        pick = sorted(good, key=lambda p: -p[key])[:45] + sorted(good, key=lambda p: -p[key] / max(p["price"], 3.9))[:25]
        pick += sorted(ps, key=lambda p: p["price"])[:10]
        pool[pos] = list({p["id"]: p for p in pick}.values())

    def feasible(sq):
        if sum(p["price"] for p in sq) > budget + 1e-9:
            return False
        c = {}
        for p in sq:
            c[p["team"]] = c.get(p["team"], 0) + 1
            if c[p["team"]] > 3:
                return False
        return True

    def cheapest():
        sq, cnt = [], {}
        for pos, n in SQUAD_SHAPE.items():
            for p in sorted(pool[pos], key=lambda p: p["price"]):
                if sum(1 for q in sq if q["pos"] == pos) >= n:
                    break
                if cnt.get(p["team"], 0) < 3:
                    sq.append(p)
                    cnt[p["team"]] = cnt.get(p["team"], 0) + 1
        return sq

    best = None
    starts = [cheapest()] + [s for s in seeds if s]
    for start in starts:
        sq = list(start)
        if not feasible(sq):
            continue
        cur = pick_xi(sq, key)[0]
        for _ in range(iters):
            i = rnd.randrange(len(sq))
            c = rnd.choice(pool[sq[i]["pos"]])
            if any(c["id"] == q["id"] for q in sq):
                continue
            cand = list(sq)
            cand[i] = c
            if not feasible(cand):
                if rnd.random() < 0.6:
                    continue
                j = rnd.randrange(len(sq))
                if j == i:
                    continue
                d = rnd.choice(pool[cand[j]["pos"]])
                if any(d["id"] == q["id"] for q in cand):
                    continue
                cand[j] = d
                if not feasible(cand):
                    continue
            s = pick_xi(cand, key)[0]
            if s > cur + 1e-9:
                sq, cur = cand, s
        if best is None or cur > best[0]:
            best = (cur, sq)
    return best


def chip_sections(M, squad, S):
    B = [("h2", "Wildcard og Free Hit")]
    ids = squad["ids"]
    cur_ids = set(ids)
    if squad.get("value"):
        budget = squad["value"] + squad["bank"]
    else:
        budget = sum(M.players[i]["price"] for i in ids) + squad["bank"]
    used = squad.get("chips_used") or []
    B.append(("p", f"Budsjett: £{f1(budget)}m (lagverdi + bank). Chips brukt så langt: "
                   f"{', '.join(used) if used else 'ingen registrert'}. Sjekk i appen hvilke chips du har igjen."))
    for title, key, label, text in (
        ("Wildcard-lag", "ep5", f"EP {len(M.gws)} r.", f"Beste lag for de neste {len(M.gws)} rundene"),
        ("Free Hit-lag", "ep1", "EP neste", f"Beste lag for kun GW{M.target} (Free Hit gjelder én runde)"),
    ):
        cur_score = pick_xi(S, key)[0]
        res = optimise_squad(M, budget, key, [S], iters=20000)
        if not res:
            continue
        score, sq = res
        _, xi, bench, cap = pick_xi(sq, key)
        changes = len(set(p["id"] for p in sq) - cur_ids)
        gain = score - cur_score
        B.append(("h3", title))
        B.append(("callout", f"{text}. Forventet {f1(score)} EP mot {f1(cur_score)} for dagens lag: "
                             f"{f1(gain).replace('-', '-') if gain < 0 else '+' + f1(gain)} EP, {changes} spillere byttes."))
        rows = []
        order = {1: 0, 2: 1, 3: 2, 4: 3}
        for role, group in (("Start", xi), ("Benk", bench)):
            for p in sorted(group, key=lambda p: (order[p["pos"]], -p[key])):
                nm = name_cell(p)
                if p is cap and role == "Start":
                    nm = H(p["name"] + " (C)", f'<b>{esc(p["name"])}</b> <span class="flag">C</span>')
                rows.append([role, nm, pos_cell(p["pos"]), M.tname(p["team"]), f1(p["price"]), f1(p[key]), fx_cell(p)])
        B.append(table(["Rolle", "Spiller", "Pos", "Lag", "£", label, "Neste kamper"], rows))
    B.append(("p", "Laget er et regnestykke, ikke en garanti: sjekk skader og pressekonferanser før du bruker en chip."))
    return B


def notify(cfg, text):
    topic = cfg.get("ntfy_topic")
    if not topic:
        return
    try:
        hdr = {"Title": "FPL-anbefaling", **({"Click": cfg["report_url"]} if cfg.get("report_url") else {})}
        try:
            req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=text.encode("utf-8"), headers={**hdr, **UA}, method="POST")
            urllib.request.urlopen(req, timeout=20).read()
        except Exception:
            curl_get(f"https://ntfy.sh/{topic}", data=text.encode("utf-8"), headers=hdr)
    except Exception as ex:
        print("Push feilet:", ex, file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-dir")
    ap.add_argument("--save-dir")
    ap.add_argument("--out-dir", default=os.path.join(PROJ, "output"))
    ap.add_argument("--only-if-new-gw", action="store_true")
    a = ap.parse_args()
    cfg_path = os.path.join(PROJ, "config.json")
    cfg = json.load(open(cfg_path, encoding="utf-8")) if os.path.exists(cfg_path) else {}
    src = Source(a.from_dir, a.save_dir)
    boot = src.get("bootstrap", "bootstrap-static/")
    state_p = os.path.join(PROJ, "work", "state.json")
    last_fin = max([e["id"] for e in boot["events"] if e.get("finished")], default=0)
    if a.only_if_new_gw:
        st = json.load(open(state_p)) if os.path.exists(state_p) else {}
        if st.get("last_gw", -1) >= last_fin:
            print(f"Ingen ny ferdigspilt runde (siste: GW{last_fin}). Hopper over.")
            return
    fixtures = src.get("fixtures", "fixtures/")
    M = Model(boot, fixtures, cfg)
    teams = cfg.get("teams") or [{"name": "meg", "team_id": cfg.get("team_id")}]
    os.makedirs(a.out_dir, exist_ok=True)
    stamp = dt.date.today().isoformat()
    for t in teams:
        tcfg = {**cfg, **t}
        lab = "".join(c for c in unicodedata.normalize("NFKD", str(t.get("name", "meg")).lower()).encode("ascii", "ignore").decode() if c.isalnum()) or "meg"
        tcfg["name"] = lab if not t.get("name") else t["name"]
        if not tcfg.get("ntfy_topic"):
            tcfg["ntfy_topic"] = os.environ.get("NTFY_" + lab.upper(), "")
        squad = load_squad(src, {**tcfg, "name": lab}, M)
        B, summary = build_report(M, squad, tcfg)
        md, hm = to_md(B), to_html(B)
        for name, txt in ((f"GW{M.target}_{lab}_{stamp}.md", md), (f"siste_{lab}.md", md), (f"siste_{lab}.html", hm)):
            with open(os.path.join(a.out_dir, name), "w", encoding="utf-8") as f:
                f.write(txt)
        print(f"Skrev rapport for {lab}, GW{M.target}")
        if summary:
            notify(tcfg, "\n".join(summary) + f"\nGW{M.target}")
    os.makedirs(os.path.dirname(state_p), exist_ok=True)
    json.dump({"last_gw": last_fin, "ran": dt.datetime.now().isoformat()}, open(state_p, "w"))

if __name__ == "__main__":
    main()
