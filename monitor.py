#!/usr/bin/env python3
"""
Monitor Santander v2  ->  avisos por Telegram
=============================================
Vigila prensa, GDELT, Reddit, Mastodon, Bluesky, canales de Telegram e Instagram
buscando cualquier evento en Santander/Cantabria o nacional que le afecte.

- Prioridad ALTA (protestas, huelgas, emergencias, movilidad) -> aviso inmediato.
- El resto -> resumen a las horas de config.yaml (el resumen llega SIEMPRE,
  aunque esté vacío: si no te llega, el monitor no está funcionando).
- Avisa si una fuente se rompe, si lleva horas sin encontrar nada o si
  el token de Instagram está a punto de caducar.
- Guarda todo en datos/eventos.csv (dataset para el trabajo).
- Comandos en el bot: /resumen  /estado  /ayuda  (responde en la siguiente pasada).

Uso:
  python monitor.py            una pasada (GitHub Actions)
  python monitor.py --bucle    pasada cada 10 min (tu PC)
  python monitor.py --seco     prueba: imprime en pantalla, no envía ni guarda nada
"""
import csv
import hashlib
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests
import yaml

BASE = Path(__file__).resolve().parent
DATOS = BASE / "datos"
ESTADO = DATOS / "estado.json"
CSV_EVENTOS = DATOS / "eventos.csv"

TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GRAPH_VER = "v25.0"
UA = {"User-Agent": "Mozilla/5.0 (monitor-santander; investigacion universitaria)"}
TIMEOUT = 20
FALLOS_PARA_AVISAR = 6          # pasadas seguidas fallando antes de avisar
HORAS_SILENCIO = 12             # sin ningún item nuevo en este tiempo -> aviso
SECO = "--seco" in sys.argv

ICONO_PRIO = {"alta": "🔴", "media": "🟠", "baja": "🔵"}
ICONO_AMBITO = {"local": "📍 Local", "nacional": "🇪🇸 Nacional", "cuenta": "👥 Cuenta"}
ORDEN_PRIO = {"alta": 3, "media": 2, "baja": 1}
STOP = set("""del las los una uno unos unas por para con sin sobre entre tras ante desde hasta
que como mas pero sus este esta estos estas ese esa han hay ser son fue era muy ya tambien
santander cantabria cantabro cantabra""".split())


# ---------------------------------------------------------------- utilidades
def ahora():
    return datetime.now(timezone.utc)


def norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s.lower()).strip()


def limpiar(s):
    s = re.sub(r"<[^>]+>", " ", str(s or ""))
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def patron(palabras):
    partes = []
    for p in palabras or []:
        p = norm(p)
        if not p:
            continue
        esc = re.escape(p)
        partes.append(rf"\b{esc}\b" if p.isdigit() else rf"\b{esc}")
    return re.compile("|".join(partes)) if partes else None


def h(s):
    return hashlib.sha1(str(s).encode()).hexdigest()[:16]


def firma(titulo):
    """Raíces de las palabras del titular (6 letras) para detectar el mismo hecho en otro medio."""
    return {w[:6] for w in re.findall(r"[a-z0-9]{3,}", norm(titulo)) if w not in STOP}


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else ""


def desde_iso(s):
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


# ---------------------------------------------------------------- telegram
def tg(texto, preview=False):
    if SECO or not TG_TOKEN:
        print("\n[TELEGRAM]\n" + re.sub(r"<[^>]+>", "", texto) + "\n")
        return True
    trozos, actual = [], ""
    for linea in texto.split("\n"):
        if len(actual) + len(linea) + 1 > 3900:
            trozos.append(actual)
            actual = ""
        actual += linea + "\n"
    trozos.append(actual)
    ok = True
    for t in trozos:
        for _ in range(3):
            try:
                r = requests.post(
                    f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                    data={"chat_id": TG_CHAT, "text": t, "parse_mode": "HTML",
                          "disable_web_page_preview": not preview},
                    timeout=TIMEOUT)
                if r.status_code == 429:
                    time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
                    continue
                if not r.ok:
                    print("Telegram respondió:", r.text)
                    ok = False
                break
            except requests.RequestException as e:
                print("Error Telegram:", e)
                ok = False
                time.sleep(2)
        time.sleep(1)
    return ok


def tg_comandos(estado):
    """Lee los mensajes que le has mandado al bot (/resumen, /estado, /ayuda)."""
    if SECO or not TG_TOKEN:
        return []
    try:
        r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                         params={"offset": estado.get("tg_offset", 0), "timeout": 0},
                         timeout=TIMEOUT)
        res = r.json().get("result", [])
    except (requests.RequestException, ValueError):
        return []
    cmds = []
    for u in res:
        estado["tg_offset"] = u["update_id"] + 1
        m = u.get("message") or {}
        if str(m.get("chat", {}).get("id")) == TG_CHAT:
            txt = (m.get("text") or "").strip().lower()
            if txt.startswith("/"):
                cmds.append(txt.split()[0].split("@")[0])
    return cmds


# ---------------------------------------------------------------- fuentes
def get(url, **kw):
    r = requests.get(url, headers=UA, timeout=kw.pop("timeout", TIMEOUT), **kw)
    r.raise_for_status()
    return r


def item(id_, titulo, texto, url, fuente, red, publicado):
    titulo = limpiar(titulo)
    texto = limpiar(texto)
    if not titulo:
        titulo = texto[:140] + ("…" if len(texto) > 140 else "")
    return {"id": id_ or url, "titulo": titulo, "texto": texto[:600], "url": url,
            "fuente": fuente, "red": red, "publicado": publicado}


def rss(url, red, fuente_def=""):
    f = feedparser.parse(get(url).content)
    if f.bozo and not f.entries:
        raise RuntimeError(f"feed no válido ({f.bozo_exception})")
    out = []
    for e in f.entries:
        fuente = (e.get("source") or {}).get("title") or fuente_def or f.feed.get("title", "")
        titulo = limpiar(e.get("title", ""))
        if fuente and titulo.endswith(" - " + fuente):
            titulo = titulo[: -len(fuente) - 3]
        pub = None
        for k in ("published_parsed", "updated_parsed"):
            if e.get(k):
                pub = datetime(*e[k][:6], tzinfo=timezone.utc)
                break
        out.append(item(e.get("id"), titulo, e.get("summary", ""), e.get("link", ""),
                        fuente, red, pub))
    return out


def google_news(q, ventana):
    url = ("https://news.google.com/rss/search?q="
           + urllib.parse.quote(f"{q} when:{ventana}") + "&hl=es&gl=ES&ceid=ES:es")
    return rss(url, "Prensa")


def gdelt(q):
    r = get("https://api.gdeltproject.org/api/v2/doc/doc", timeout=40, params={
        "query": q, "mode": "artlist", "format": "json", "maxrecords": 75,
        "timespan": "1d", "sort": "datedesc"})
    try:
        data = r.json()
    except ValueError:
        if "limit" in r.text.lower():
            raise RuntimeError("GDELT pide esperar (límite de peticiones)")
        data = {}
    out = []
    for a in data.get("articles", []):
        try:
            pub = datetime.strptime(a.get("seendate", ""), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            pub = None
        out.append(item(a.get("url"), a.get("title", ""), "", a.get("url", ""),
                        a.get("domain", ""), "Prensa (GDELT)", pub))
    return out


def telegram_canal(nombre):
    txt = get(f"https://t.me/s/{nombre}").text
    out = []
    for bloque in txt.split('class="tgme_widget_message_wrap')[1:]:
        post = re.search(r'data-post="([^"]+)"', bloque)
        cuerpo = re.search(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', bloque, re.S)
        fecha = re.search(r'<time[^>]+datetime="([^"]+)"', bloque)
        if not post or not cuerpo:
            continue
        pub = desde_iso(fecha.group(1)) if fecha else None
        out.append(item(post.group(1), "", cuerpo.group(1), f"https://t.me/{post.group(1)}",
                        f"@{nombre}", "Telegram", pub))
    return out


def _bsky_post(p):
    rec = p.get("record", {})
    handle = p.get("author", {}).get("handle", "")
    rkey = p.get("uri", "").rsplit("/", 1)[-1]
    return item(p.get("uri"), "", rec.get("text", ""),
                f"https://bsky.app/profile/{handle}/post/{rkey}",
                f"@{handle}", "Bluesky", desde_iso(rec.get("createdAt", "").replace("Z", "+00:00")))


def bluesky_cuenta(handle):
    r = get("https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed",
            params={"actor": handle, "limit": 20})
    return [_bsky_post(x["post"]) for x in r.json().get("feed", [])]


_BSKY_JWT = None


def bluesky_busqueda(q):
    global _BSKY_JWT
    hnd, pwd = os.getenv("BSKY_HANDLE", ""), os.getenv("BSKY_APP_PASSWORD", "")
    if not (hnd and pwd):
        return None   # sin credenciales -> se salta
    if _BSKY_JWT is None:
        r = requests.post("https://bsky.social/xrpc/com.atproto.server.createSession",
                          json={"identifier": hnd, "password": pwd}, timeout=TIMEOUT)
        r.raise_for_status()
        _BSKY_JWT = r.json()["accessJwt"]
    r = requests.get("https://bsky.social/xrpc/app.bsky.feed.searchPosts",
                     params={"q": q, "sort": "latest", "limit": 50, "lang": "es"},
                     headers={**UA, "Authorization": f"Bearer {_BSKY_JWT}"}, timeout=TIMEOUT)
    r.raise_for_status()
    return [_bsky_post(p) for p in r.json().get("posts", [])]


def instagram(usuario):
    ig_id, token = os.getenv("IG_USER_ID", ""), os.getenv("META_TOKEN", "")
    if not (ig_id and token):
        return None
    campos = (f"business_discovery.username({usuario})"
              "{username,media.limit(10){caption,permalink,timestamp}}")
    r = requests.get(f"https://graph.facebook.com/{GRAPH_VER}/{ig_id}",
                     params={"fields": campos, "access_token": token}, timeout=TIMEOUT)
    data = r.json()
    if "error" in data:
        msg = data["error"].get("message", "error")
        if "business" in msg.lower() or data["error"].get("code") == 110:
            msg = "la cuenta no es Business/Creator o no existe: " + msg
        raise RuntimeError(msg)
    out = []
    for m in data.get("business_discovery", {}).get("media", {}).get("data", []):
        try:
            pub = datetime.strptime(m.get("timestamp", ""), "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            pub = None
        out.append(item(m.get("id"), "", m.get("caption", ""), m.get("permalink", ""),
                        f"@{usuario}", "Instagram", pub))
    return out


def tareas(cfg, estado):
    """Lista de (clave, función, requiere_zona, ámbito_base)."""
    f = cfg.get("fuentes", {})
    gn = f.get("google_news", {})
    ventana = gn.get("ventana", "1d")
    t = []
    for q in gn.get("locales", []):
        t.append((f"gnews:{q}", lambda q=q: google_news(q, ventana), True, "local"))
    for q in gn.get("nacionales", []):
        t.append((f"gnews-nac:{q}", lambda q=q: google_news(q, ventana), False, "nacional"))
    for u in f.get("rss_medios", []):
        t.append((f"rss:{u}", lambda u=u: rss(u, "Prensa local"), False, "local"))
    for i, q in enumerate(f.get("gdelt", [])):
        def g(q=q, i=i):
            if i:
                time.sleep(6)
            return gdelt(q)
        t.append((f"gdelt:{q}", g, True, "local"))
    for s in f.get("reddit", []):
        t.append((f"reddit:{s}", lambda s=s: rss(f"https://www.reddit.com/r/{s}/new/.rss",
                                                 "Reddit", f"r/{s}"), False, "local"))
    for m in f.get("mastodon_hashtags", []):
        srv, tag = m.split("/", 1)
        t.append((f"mastodon:{m}", lambda s=srv, tg_=tag: rss(f"https://{s}/tags/{tg_}.rss",
                                                              "Mastodon", f"#{tg_}"), True, "local"))
    for q in f.get("bluesky_busquedas", []):
        t.append((f"bsky:{q}", lambda q=q: bluesky_busqueda(q), True, "local"))
    for a in f.get("bluesky_cuentas", []):
        t.append((f"bsky-cuenta:{a}", lambda a=a: bluesky_cuenta(a), False, "cuenta"))
    for c in f.get("telegram_canales", []):
        t.append((f"tgcanal:{c}", lambda c=c: telegram_canal(c), False, "cuenta"))
    ig = f.get("instagram", {}) or {}
    ult = desde_iso(estado.get("ig_ultimo"))
    if not ult or ahora() - ult >= timedelta(minutes=ig.get("intervalo_min", 30)):
        if ig.get("cuentas") and os.getenv("META_TOKEN"):
            estado["ig_ultimo"] = iso(ahora())
        for u in ig.get("cuentas", []):
            t.append((f"ig:{u}", lambda u=u: instagram(u), False, "cuenta"))
    return t


# ---------------------------------------------------------------- clasificación
class Clasificador:
    def __init__(self, cfg):
        self.zona = patron(cfg.get("zona", []))
        self.excluir = patron(cfg.get("excluir", []))
        self.temas = {}
        for nombre, d in (cfg.get("temas") or {}).items():
            p = patron(d.get("palabras", []))
            if p:
                self.temas[nombre] = (p, d.get("prioridad", "media"))

    def __call__(self, it, requiere_zona, ambito_base):
        t = norm(it["titulo"] + " " + it["texto"])
        temas = [n for n, (p, _) in self.temas.items() if p.search(t)]
        zonas = {m.group(0) for m in self.zona.finditer(t)} if self.zona else set()
        if zonas and self.excluir and self.excluir.search(t) and zonas <= {"santander"}:
            zonas = set()                       # Banco Santander, Colombia...
        if ambito_base == "cuenta":
            ambito = "cuenta"
        elif zonas:
            ambito = "local"
        elif requiere_zona:
            return None
        elif ambito_base == "nacional":
            if not temas:
                return None
            ambito = "nacional"
        else:
            ambito = "local"                    # fuente local por naturaleza
        if temas:
            prio = max((self.temas[n][1] for n in temas), key=lambda p: ORDEN_PRIO.get(p, 0))
        else:
            prio = "media" if ambito == "cuenta" else "baja"
        return {"temas": temas or ["otros"], "prioridad": prio, "ambito": ambito}


# ---------------------------------------------------------------- estado / datos
def cargar_estado():
    if ESTADO.exists():
        try:
            return json.loads(ESTADO.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {}


def guardar_estado(estado):
    if SECO:
        return
    DATOS.mkdir(exist_ok=True)
    corte = iso(ahora() - timedelta(days=7))
    estado["vistos"] = {k: v for k, v in estado.get("vistos", {}).items() if v >= corte}
    estado["titulos"] = estado.get("titulos", [])[-800:]
    estado["pendientes"] = estado.get("pendientes", [])[-600:]
    ESTADO.write_text(json.dumps(estado, ensure_ascii=False, indent=0), encoding="utf-8")


def guardar_csv(filas):
    if SECO or not filas:
        return
    DATOS.mkdir(exist_ok=True)
    nuevo = not CSV_EVENTOS.exists()
    with CSV_EVENTOS.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if nuevo:
            w.writerow(["detectado_utc", "publicado_utc", "ambito", "prioridad", "temas",
                        "red", "fuente", "titulo", "url", "alertado"])
        w.writerows(filas)


# ---------------------------------------------------------------- mensajes
def hora_local(dt, tz):
    if not dt:
        return ""
    d = dt.astimezone(tz)
    hoy = ahora().astimezone(tz).date()
    return d.strftime("%H:%M") if d.date() == hoy else d.strftime("%d/%m %H:%M")


def msg_alerta(it, c, tz):
    temas = ", ".join(x.upper() for x in c["temas"])
    hora = hora_local(it["publicado"], tz)
    return (f"{ICONO_PRIO[c['prioridad']]} <b>{html.escape(temas)}</b> · {ICONO_AMBITO[c['ambito']]}\n"
            f"<b>{html.escape(it['titulo'][:300])}</b>\n"
            f"{html.escape(it['fuente'])} · {it['red']}{' · ' + hora if hora else ''}\n"
            f"{html.escape(it['url'])}")


def msg_resumen(estado, cfg, tz, titulo="Resumen"):
    pend = estado.get("pendientes", [])
    n_max = cfg.get("max_por_tema_en_resumen", 12)
    cab = f"🗞 <b>{titulo} · {ahora().astimezone(tz).strftime('%d/%m %H:%M')}</b>"
    if not pend:
        lineas = [cab, "Sin novedades desde el último resumen. El monitor sigue funcionando ✅"]
    else:
        orden = list((cfg.get("temas") or {}).keys()) + ["otros"]
        grupos = {}
        for p in pend:
            grupos.setdefault(p["temas"][0], []).append(p)
        lineas = [cab, f"{len(pend)} novedades · 🔔 = ya avisado"]
        for tema in orden:
            items = grupos.get(tema)
            if not items:
                continue
            items.sort(key=lambda x: (ORDEN_PRIO.get(x["prioridad"], 0), x["ts"]), reverse=True)
            lineas.append(f"\n<b>{tema.upper()}</b> ({len(items)})")
            for p in items[:n_max]:
                amb = {"local": "📍", "nacional": "🇪🇸", "cuenta": "👥"}[p["ambito"]]
                lineas.append(f"{'🔔' if p.get('alertado') else '•'}{amb} "
                              f"<a href=\"{html.escape(p['url'])}\">{html.escape(p['titulo'][:140])}</a>"
                              f" <i>({html.escape(p['fuente'][:30])})</i>")
            if len(items) > n_max:
                lineas.append(f"  … y {len(items) - n_max} más")
    rotas = [k for k, v in estado.get("salud", {}).items() if v.get("fallos", 0) >= FALLOS_PARA_AVISAR]
    if rotas:
        lineas.append(f"\n⚠️ Fuentes con fallos: {html.escape(', '.join(r.split(':', 1)[-1][:40] for r in rotas))}")
    return "\n".join(lineas)


def msg_estado(estado, n_tareas):
    salud = estado.get("salud", {})
    ok = sum(1 for v in salud.values() if v.get("fallos", 0) == 0)
    malas = [(k, v) for k, v in salud.items() if v.get("fallos", 0)]
    lineas = [f"🩺 <b>Estado del monitor</b>",
              f"Fuentes activas: {n_tareas} · funcionando: {ok}",
              f"Última pasada: {estado.get('ultima_pasada', '-')}",
              f"Último hallazgo: {estado.get('ultimo_item', '-')}",
              f"Pendientes para el resumen: {len(estado.get('pendientes', []))}"]
    for k, v in malas:
        lineas.append(f"⚠️ {html.escape(k[:60])}: {v['fallos']} fallos — {html.escape(v.get('error', '')[:120])}")
    faltan = [n for n, var in [("Instagram", "META_TOKEN"), ("Bluesky", "BSKY_HANDLE")]
              if not os.getenv(var)]
    if faltan:
        lineas.append("\nSin configurar (opcional): " + ", ".join(faltan))
    return "\n".join(lineas)


AYUDA = ("🤖 <b>Comandos</b>\n/resumen – resumen ahora\n/estado – salud de las fuentes\n"
         "/ayuda – esta ayuda\n\nResponde en la siguiente pasada (10–20 min).")


# ---------------------------------------------------------------- pasada
def pasada():
    cfg = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8"))
    tz = ZoneInfo(cfg.get("zona_horaria", "Europe/Madrid"))
    estado = cargar_estado()
    primera = not estado.get("vistos")
    estado.setdefault("vistos", {})
    estado.setdefault("salud", {})
    clasificar = Clasificador(cfg)
    max_edad = timedelta(hours=cfg.get("antiguedad_max_horas", 36))
    ts_ahora = iso(ahora())

    # titulares recientes para detectar duplicados entre medios
    recientes = [(set(x[0]), x[1]) for x in estado.get("titulos", [])
                 if x[1] >= iso(ahora() - timedelta(hours=48))]

    alertas, pendientes_nuevos, filas_csv, avisos_sistema = [], [], [], []
    lista = tareas(cfg, estado)
    for clave, fn, requiere_zona, ambito_base in lista:
        s = estado["salud"].setdefault(clave, {"fallos": 0})
        try:
            items = fn()
        except Exception as e:                                   # noqa: BLE001
            s["fallos"] = s.get("fallos", 0) + 1
            s["error"] = f"{type(e).__name__}: {e}"[:200]
            print(f"✗ {clave}: {s['error']}")
            if s["fallos"] == FALLOS_PARA_AVISAR and not s.get("avisado"):
                s["avisado"] = True
                avisos_sistema.append(f"⚠️ La fuente <b>{html.escape(clave)}</b> lleva "
                                      f"{s['fallos']} pasadas fallando:\n{html.escape(s['error'])}")
            continue
        if items is None:                                        # sin credenciales
            estado["salud"].pop(clave, None)
            continue
        if s.get("avisado"):
            avisos_sistema.append(f"✅ La fuente <b>{html.escape(clave)}</b> vuelve a funcionar.")
        estado["salud"][clave] = {"fallos": 0, "ultimo_ok": ts_ahora}
        print(f"✓ {clave}: {len(items)}")

        for it in items:
            if not it["url"] or not it["titulo"]:
                continue
            k = h(it["id"])
            ku = h(it["url"])
            if k in estado["vistos"] or ku in estado["vistos"]:
                continue
            estado["vistos"][k] = estado["vistos"][ku] = ts_ahora
            if it["publicado"] and ahora() - it["publicado"] > max_edad:
                continue
            c = clasificar(it, requiere_zona, ambito_base)
            if not c:
                continue
            fir = firma(it["titulo"])
            if len(fir) >= 4 and any(len(fir & r) / len(fir | r) >= 0.6 for r, _ in recientes):
                continue                                         # mismo hecho, otro medio
            recientes.append((fir, ts_ahora))
            estado.setdefault("titulos", []).append([sorted(fir), ts_ahora])
            estado["ultimo_item"] = ts_ahora
            if primera:
                continue
            alertar = c["prioridad"] == "alta"
            pendientes_nuevos.append({"titulo": it["titulo"], "url": it["url"], "fuente": it["fuente"],
                                      "temas": c["temas"], "prioridad": c["prioridad"],
                                      "ambito": c["ambito"], "ts": ts_ahora, "alertado": False})
            if alertar:
                alertas.append((it, c, pendientes_nuevos[-1]))
            filas_csv.append([ts_ahora, iso(it["publicado"]), c["ambito"], c["prioridad"],
                              "|".join(c["temas"]), it["red"], it["fuente"], it["titulo"], it["url"],
                              ""])

    # ---- envíos
    if primera:
        tg("✅ <b>Monitor Santander v2 arrancado</b>\n"
           f"{len(lista)} fuentes activas. A partir de ahora te aviso de lo nuevo.\n"
           f"Resúmenes a las {', '.join(str(x) + ':00' for x in cfg.get('horas_resumen', []))}.\n\n" + AYUDA)
        estado["ultimo_resumen"] = ts_ahora

    max_al = cfg.get("max_alertas_por_pasada", 10)
    alertas.sort(key=lambda a: (a[1]["ambito"] != "local", a[1]["ambito"] == "nacional"))
    for it, c, p in alertas[:max_al]:
        if tg(msg_alerta(it, c, tz), preview=True):
            p["alertado"] = True
    if len(alertas) > max_al:
        tg(f"➕ Hay {len(alertas) - max_al} avisos importantes más. Te van en el próximo resumen.")
    for f in filas_csv:
        f[-1] = "si" if any(p["url"] == f[8] and p["alertado"] for p in pendientes_nuevos) else ""
    estado.setdefault("pendientes", []).extend(pendientes_nuevos)

    for a in avisos_sistema:
        tg(a)

    # pico de actividad por tema (local)
    hist = estado.setdefault("hist_temas", {})
    for p in pendientes_nuevos:
        if p["ambito"] == "local":
            for t in p["temas"]:
                hist.setdefault(t, []).append(ts_ahora)
    for t, lista_ts in hist.items():
        lista_ts[:] = [x for x in lista_ts if x >= iso(ahora() - timedelta(hours=24))]
        ult2 = sum(1 for x in lista_ts if x >= iso(ahora() - timedelta(hours=2)))
        base = (len(lista_ts) - ult2) / 11 or 0.5            # media cada 2h en las 22h previas
        ult_aviso = desde_iso(estado.get("pico_avisado", {}).get(t))
        if t != "otros" and ult2 >= 6 and ult2 >= 3 * base and (not ult_aviso or ahora() - ult_aviso > timedelta(hours=6)):
            estado.setdefault("pico_avisado", {})[t] = ts_ahora
            tg(f"📈 <b>Pico de actividad: {html.escape(t.upper())}</b>\n"
               f"{ult2} noticias/publicaciones locales en las últimas 2 h (lo normal: ~{base:.0f}). "
               "Puede estar pasando algo.")

    # silencio prolongado
    ult = desde_iso(estado.get("ultimo_item"))
    aviso_sil = desde_iso(estado.get("silencio_avisado"))
    if not primera and ult and ahora() - ult > timedelta(hours=HORAS_SILENCIO) and (
            not aviso_sil or ahora() - aviso_sil > timedelta(hours=HORAS_SILENCIO)):
        estado["silencio_avisado"] = ts_ahora
        tg(f"🤔 Llevo más de {HORAS_SILENCIO} h sin encontrar nada nuevo. "
           "Puede que alguna fuente haya cambiado. Escribe /estado para ver el detalle.")

    # token de Instagram a punto de caducar
    cad = str((cfg.get("fuentes", {}).get("instagram") or {}).get("token_caduca") or "")
    if cad:
        try:
            dias = (datetime.fromisoformat(cad).date() - ahora().astimezone(tz).date()).days
            if dias <= 7 and estado.get("token_avisado") != ahora().astimezone(tz).date().isoformat():
                estado["token_avisado"] = ahora().astimezone(tz).date().isoformat()
                tg(f"🔑 El token de Instagram caduca en {dias} días ({cad}). Renuévalo en Meta "
                   "y actualiza el secret META_TOKEN y la fecha en config.yaml.")
        except ValueError:
            pass

    # comandos y resúmenes programados
    cmds = tg_comandos(estado)
    if "/estado" in cmds:
        tg(msg_estado(estado, len(lista)))
    if "/ayuda" in cmds or "/start" in cmds:
        tg(AYUDA)
    hoy = ahora().astimezone(tz)
    toca = None
    for hr in sorted(cfg.get("horas_resumen", [])):
        slot = hoy.replace(hour=hr, minute=0, second=0, microsecond=0)
        if hoy >= slot and (desde_iso(estado.get("ultimo_resumen")) or slot - timedelta(1)) < slot:
            toca = slot
    if not primera and (toca or "/resumen" in cmds):
        if tg(msg_resumen(estado, cfg, tz, "Resumen" if toca else "Resumen a petición")):
            estado["pendientes"] = []
            estado["ultimo_resumen"] = ts_ahora

    estado["ultima_pasada"] = hoy.strftime("%d/%m %H:%M")
    guardar_csv(filas_csv)
    guardar_estado(estado)
    print(f"Pasada: {len(pendientes_nuevos)} nuevos, {len(alertas)} alertas")


def main():
    if not SECO and not (TG_TOKEN and TG_CHAT):
        sys.exit("Faltan TELEGRAM_TOKEN / TELEGRAM_CHAT_ID (secrets o variables de entorno).")
    while True:
        try:
            pasada()
        except Exception as e:                                   # noqa: BLE001
            print("ERROR:", repr(e))
            marca = DATOS / "ultimo_error.txt"
            try:
                previo = desde_iso(marca.read_text().strip()) if marca.exists() else None
            except OSError:
                previo = None
            if not previo or ahora() - previo > timedelta(hours=6):   # máx. 1 aviso cada 6 h
                tg(f"❌ <b>El monitor ha fallado</b>\n{html.escape(repr(e))[:500]}")
                if not SECO:
                    DATOS.mkdir(exist_ok=True)
                    marca.write_text(iso(ahora()))
            if "--bucle" not in sys.argv:
                raise
        if "--bucle" not in sys.argv:
            break
        time.sleep(600)


if __name__ == "__main__":
    main()
