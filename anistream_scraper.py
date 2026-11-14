#!/usr/bin/env python3
# anistream_scraper.py  —  AniStream → AniLib  (versão otimizada)
# Requisito: pip install requests

import requests, json, argparse, sys, secrets, re, glob, os, random, threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE            = "https://app.anistream.biz"
KNOWN_DEVICE_ID = "a9d0ebde-e77e-48d6-a6ee-cbfa9c991c1f"
# AudioStreamIndex é definido dinamicamente por get_playback_info()
# VideoBitrate baixado para 8 Mbps — força transcode pelo Jellyfin (resolve áudio AC3→AAC na maioria dos casos)
STREAM_BASE     = (
    "VideoCodec=hevc,h264&AudioCodec=aac"
    "&VideoBitrate=8000000&AudioBitrate=192000"
    "&MaxFramerate=23.976025&SegmentContainer=ts&MinSegments=2"
    "&TranscodingMaxAudioChannels=6&RequireAvc=false"
    "&EnableAudioVbrEncoding=true&VideoStreamIndex=0"
)
LIBRARY_FILE = "biblioteca_completa.json"
SAVE_EVERY   = 10   # salva a cada N animes (menos I/O)

import base64 as _b64, time
def _d(s): return _b64.b64decode(s).decode()

# ── filtros NSFW ──────────────────────────────────────────────────────────────

NSFW_GENRES = {_d(x) for x in [
    "aGVudGFp","YWR1bHQgYW5pbWF0aW9u","YWR1bHRzIG9ubHk=","eHh4",
    "ZXJvZ2U=","YWR1bHQ=","cG9ybm9ncmFwaHk=","ZXhwbGljaXQ=","ZXJvdGlj"
]}

# "sex"/"yuri"/"yaoi" removidos — genéricos demais, causam falsos positivos
NSFW_TAGS = {_d(x) for x in [
    "aGVudGFp","ZXhwbGljaXQgY29udGVudA==","YWR1bHRzIG9ubHk=","bnNmdw==",
    "bnVkaXR5","c2V4dWFsIGNvbnRlbnQ=","cG9ybm9ncmFwaGlj","ZXJvdGlj",
    "aW5jZXN0","ZnV0YW5hcmk=","YWhlZ2Fv","Y3JlYW1waWU=","dmFnaW5hbA==",
    "YW5hbA==","b3Jnb3Vzbw==","aGFyZGNvcmU=","dGVudGFjbGU=",
    "dW5jZW5zb3JlZA==","bG9saQ==","c2hvdGE=","YmxhY2ttYWls",
    "dmFnaW5h","cGVuaXM=","Y3Vt"
]}

NSFW_KEYWORDS = {_d(x) for x in [
    "aGVudGFp","ZXJvdGlj","eHh4","dW5jZW5zb3JlZA==","YWhlZ2Fv",
    "Y3JlYW1waWU=","ZnV0YW5hcmk=","aGFyZGNvcmU=","dGVudGFjbGU="
]}

NSFW_TITLE_BLOCKS = {_d(x) for x in [
    "SGlnaCBTY2hvb2wgRHhE","SW50ZXJzcGVjaWVzIFJldmlld2Vycw==",
    "WW9zdWdhIG5vIFNvcmE=","S2lzcyB4IFNpcw==","TWFrZW4tS2k=",
    "U2hpbW9uZXRh","VmFsa3lyaWUgRHJpdmU=","UHJpc29uIFNjaG9vbA==",
    "SWtraSBUb3VzZW4=","U2Vpa29uIG5vIFF3YXNlcg==","UXVlZW4ncyBCbGFkZQ==",
    "TGFkaWVzIHZlcnN1cyBCdXRsZXJz","U29mdGVubmk=","Um8tS3l1LUJ1",
    "RXJvbWFuZ2E=","TmFuZGUgS29rbyBuaSBTZW5zZWk=","U2tpcnQgbm8gTmFrYQ==",
    "T3ZlcmZsb3c=","RG9tZXN0aWMgbmEgS2FuZWpv","UmVkbyBvZiBIZWFsZXI=",
]}

NSFW_WHITELIST_SUBS = [_d(x) for x in [
    "TXVzaG9rdSBUZW5zZWk=",           # Mushoku Tensei
    "R29ibGluIFNsYXllcg==",           # Goblin Slayer
    "Q2hhcmxvdHRl",                   # Charlotte
    "R3JlYXQgUHJldGVuZGVy",          # Great Pretender
    "Tm9yYWdhbWk=",                   # Noragami
    "TWFkZSBpbiBBYnlzcw==",           # Made in Abyss (séries e filmes)
    "VGhlIFByb21pc2VkIE5ldmVybGFuZA==", # The Promised Neverland
    "TXkgRHJlc3MtVXAgRGFybGluZw==",  # My Dress-Up Darling
    "S29yZSB3YSBab21iaWU=",           # Kore wa Zombie Desu ka?
    "S29pIHRvIFNlbmt5bw==",           # Koi to Senkyo to Chocolate
    "SGVsbCBNb2Rl",                   # Hell Mode (título usa "hardcore" como dificuldade de jogo)
]]

def _title_blocked(name):
    nl = name.lower()
    return any(b.lower() in nl for b in NSFW_TITLE_BLOCKS)

def _whitelisted(name):
    nl = name.lower()
    return any(w.lower() in nl for w in NSFW_WHITELIST_SUBS)

def is_nsfw(serie):
    name = serie.get("Name", "")
    if _whitelisted(name): return False
    genres = {g.lower() for g in serie.get("Genres", [])}
    tags   = {t.lower() for t in serie.get("Tags", [])}
    rating = (serie.get("OfficialRating") or "").upper()
    if genres & NSFW_GENRES:                              return True
    if tags   & NSFW_TAGS:                               return True
    if rating in ("X", "NC-17", "AO", "XXX"):            return True
    if any(kw in name.lower() for kw in NSFW_KEYWORDS):  return True
    if _title_blocked(name):                              return True
    return False

# ── helpers ───────────────────────────────────────────────────────────────────

def new_uid():
    return "id-" + format(random.getrandbits(32), '08x') + format(int(secrets.randbits(16)), '04x')

def dash(s):
    s = s.replace("-", "")
    if len(s) != 32: return s
    return f"{s[:8]}-{s[8:12]}-{s[12:16]}-{s[16:20]}-{s[20:]}"

def item_id_from_url(url):
    m = re.search(r'/videos/([0-9a-f-]{32,36})/', url, re.I)
    return m.group(1).replace("-", "") if m else None

# ── biblioteca ────────────────────────────────────────────────────────────────

_save_lock   = threading.Lock()
_pending_buf = []    # animes aguardando save (acesso sempre dentro do lock)
_total_saved = 0     # contador thread-safe de animes na biblioteca + buffer

def load_library():
    if os.path.exists(LIBRARY_FILE):
        try:
            with open(LIBRARY_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if "animes" in data: return data
        except Exception:
            pass
    return {"app":"AniLib","author":"Heitor","license":"Open Source",
            "exportedAt":"","version":1,"animes":[]}

def save_library(lib, force=False):
    """Thread-safe. Salva a cada SAVE_EVERY ou quando force=True."""
    global _pending_buf, _total_saved
    with _save_lock:
        if not force and len(_pending_buf) < SAVE_EVERY:
            return
        lib["animes"].extend(_pending_buf)
        _pending_buf = []
        lib["exportedAt"] = datetime.now(timezone.utc).isoformat()
        lib["animes"].sort(key=lambda a: a.get("name","").lower())
        tmp = LIBRARY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(lib, f, indent=2, ensure_ascii=False)
        os.replace(tmp, LIBRARY_FILE)
        _total_saved = len(lib["animes"])

def queue_anime(lib, anime):
    """Enfileira anime e salva quando buffer cheio. Retorna contagem atual."""
    global _pending_buf, _total_saved
    with _save_lock:
        _pending_buf.append(anime)
        _total_saved += 1
        count = _total_saved
        should_save = len(_pending_buf) >= SAVE_EVERY
    if should_save:
        save_library(lib)
    return count

def flush_library(lib):
    save_library(lib, force=True)

def library_names(lib):
    global _total_saved
    _total_saved = len(lib["animes"])
    return {a["name"].lower() for a in lib["animes"]}

# ── API ───────────────────────────────────────────────────────────────────────

def make_session(key):
    """Sessão com connection pool e retry no transporte."""
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,          # 0.5 → 1 → 2 s entre tentativas
        status_forcelist=[500,502,503,504],
        allowed_methods=["GET","POST"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=30,
        pool_maxsize=30,
        pool_block=False,
    )
    s.mount("https://", adapter)
    s.mount("http://",  adapter)
    s.headers.update({
        "User-Agent":    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36",
        "Authorization": (
            f'MediaBrowser Token="{key}", Client="Jellyfin Web", '
            f'Device="Chrome", DeviceId="{KNOWN_DEVICE_ID}", Version="10.9.11"'
        ),
        "Accept": "application/json",
        "Connection": "keep-alive",
    })
    return s

def api_get(session, path, _retries=3, **params):
    """GET com jitter para evitar thundering herd quando vários workers falham juntos."""
    last_exc = None
    for attempt in range(_retries):
        try:
            r = session.get(BASE + path, params=params, timeout=(8, 40))
            # (8s connect, 40s read)
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError) as e:
            last_exc = e
            time.sleep(0.5 + random.random())  # jitter 0.5–1.5 s
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code >= 500:
                last_exc = e
                time.sleep(0.5 + random.random())
            else:
                raise
    raise last_exc

def get_user_id(session):
    for path in ("/Users/Me", "/Users", "/Sessions"):
        try:
            data = api_get(session, path)
            if isinstance(data, dict) and data.get("Id"): return data["Id"]
            if isinstance(data, list):
                for item in data:
                    uid = item.get("Id") or item.get("UserId")
                    if uid: return uid
        except Exception:
            pass
    return None

def get_all_series(session, user_id):
    all_items, start, batch = [], 0, 500
    print("  puxando catálogo...", end="", flush=True)
    while True:
        data = api_get(session, f"/Users/{user_id}/Items",
                       IncludeItemTypes="Series,Movie",
                       Recursive="true",
                       Fields="Genres,Tags,OfficialRating,ProductionYear,Overview",
                       SortBy="SortName",
                       SortOrder="Ascending",
                       Limit=batch,
                       StartIndex=start)
        items = data.get("Items", [])
        total = data.get("TotalRecordCount", 0)
        all_items.extend(items)
        start += batch
        print(f" {len(all_items)}/{total}", end="", flush=True)
        if start >= total or not items: break
    print()
    return all_items

MAX_EPS = 2000   # nenhum anime real tem mais que isso; acima = dado corrompido no Jellyfin

def get_episodes(session, user_id, series_id):
    all_eps, start, batch = [], 0, 1000
    while True:
        data = api_get(session, f"/Users/{user_id}/Items",
                       ParentId=series_id,
                       IncludeItemTypes="Episode",
                       Recursive="true",
                       Fields="Id,ParentIndexNumber,IndexNumber,Name",
                       SortBy="ParentIndexNumber,IndexNumber",
                       SortOrder="Ascending",
                       Limit=batch,
                       StartIndex=start)
        items = data.get("Items", [])
        total = data.get("TotalRecordCount", 0)
        # dado claramente corrompido — aborta
        if total > MAX_EPS:
            print(f"    ⚠ {total} eps detectados — parece dado corrompido, pulando série", flush=True)
            return []
        all_eps.extend(items)
        start += batch
        if start >= total or not items: break
        print(f"    … {len(all_eps)}/{total} eps", flush=True)
    return all_eps

def _best_audio_index(streams):
    """Escolhe o melhor AudioStreamIndex a partir da lista de MediaStreams do Jellyfin.
    Prioridade:
      1. stream de áudio PT marcado como IsDefault
      2. qualquer stream de áudio PT
      3. stream de áudio marcado como IsDefault (japonês/outro)
      4. primeiro stream de áudio encontrado
    Detecta PT tanto pelo campo Language quanto por Title/DisplayTitle.
    Retorna o Index (int) ou None se não houver streams de áudio.
    """
    audio = [s for s in streams if s.get("Type") == "Audio"]
    if not audio:
        return None

    PT_LANGS = {"por", "pt", "ptbr", "ptbr", "portuguese", "port", "pb", "pt-br", "pt_br"}
    PT_WORDS = ("portugu", "brasil", "portuguese", "pt-br", "ptbr")

    def is_pt(s):
        # verifica campo Language (ISO 639)
        lang = (s.get("Language") or "").lower().replace("-", "").replace("_", "")
        if lang in PT_LANGS:
            return True
        # verifica Title e DisplayTitle (ex: "Português", "Portuguese", "Português (Brasil)")
        for field in ("Title", "DisplayTitle", "Label"):
            t = (s.get(field) or "").lower()
            if any(w in t for w in PT_WORDS):
                return True
        return False

    # 1 — default PT
    for s in audio:
        if s.get("IsDefault") and is_pt(s):
            return s["Index"]
    # 2 — qualquer PT
    for s in audio:
        if is_pt(s):
            return s["Index"]
    # 3 — default (sem PT disponível → original/japonês)
    for s in audio:
        if s.get("IsDefault"):
            return s["Index"]
    # 4 — primeiro encontrado
    return audio[0]["Index"]


def _log_audio(streams, aidx, source=""):
    audio = [s for s in streams if s.get("Type") == "Audio"]
    if not audio:
        return
    info = ", ".join(
        f"[{s.get('Index')}] lang={s.get('Language','?')!r} "
        f"title={s.get('Title','')!r} default={s.get('IsDefault',False)}"
        for s in audio
    )
    tag = f" ({source})" if source else ""
    print(f"      áudio{tag}: {info}  → escolhido={aidx}", flush=True)


def get_playback_info(session, user_id, item_id):
    """Retorna (media_source_id, tag, audio_stream_index).
    Estratégia: GET /Items/{id}?Fields=MediaStreams primeiro (simples, sem DeviceProfile),
    depois POST PlaybackInfo como fallback.
    """
    iid = dash(item_id)

    # ── 1. GET /Items/{id}?Fields=MediaStreams (mais confiável) ──────────────
    try:
        r = session.get(
            f"{BASE}/Items/{iid}",
            params={"Fields": "MediaStreams"},
            timeout=(5, 15),
        )
        if r.ok:
            data    = r.json()
            # MediaStreams pode estar direto no item ou dentro de MediaSources
            streams = data.get("MediaStreams") or []
            if not streams:
                for src in data.get("MediaSources", []):
                    streams = src.get("MediaStreams") or []
                    if streams: break
            if streams:
                aidx = _best_audio_index(streams)
                _log_audio(streams, aidx, "GET")
                return item_id, "", aidx
    except Exception as e:
        print(f"      GET /Items falhou: {e}", flush=True)

    # ── 2. fallback: POST PlaybackInfo ───────────────────────────────────────
    try:
        r = session.post(
            f"{BASE}/Items/{iid}/PlaybackInfo",
            params={"UserId": user_id},
            json={
                "DeviceProfile": {
                    "DirectPlayProfiles": [],
                    "TranscodingProfiles": [{
                        "Container": "ts", "Type": "Video",
                        "AudioCodec": "aac", "VideoCodec": "h264",
                        "Context": "Streaming", "Protocol": "hls",
                    }],
                    "CodecProfiles": [], "SubtitleProfiles": [],
                }
            },
            timeout=(8, 20),
        )
        if r.ok:
            sources = r.json().get("MediaSources", [])
            if sources:
                src     = sources[0]
                msid    = src.get("Id", item_id)
                tag     = src.get("ETag", "")
                streams = src.get("MediaStreams", [])
                aidx    = _best_audio_index(streams)
                _log_audio(streams, aidx, "POST")
                return msid, tag, aidx
            print(f"      PlaybackInfo: MediaSources vazio para {item_id}", flush=True)
        else:
            print(f"      PlaybackInfo FALHOU: HTTP {r.status_code} para {item_id}", flush=True)
    except Exception as e:
        print(f"      PlaybackInfo ERRO: {e}", flush=True)

    return item_id, "", None


def build_url(item_id, media_source_id, key, play_session, tag="", audio_idx=None):
    iid  = dash(item_id.replace("-", ""))
    msid = media_source_id.replace("-", "")
    # fallback: índice 1 se PlaybackInfo não retornou nada
    aidx = audio_idx if audio_idx is not None else 1
    url  = (
        f"{BASE}/videos/{iid}/main.m3u8"
        f"?DeviceId={KNOWN_DEVICE_ID}"
        f"&MediaSourceId={msid}"
        f"&{STREAM_BASE}"
        f"&AudioStreamIndex={aidx}"
        f"&PlaySessionId={play_session}"
        f"&ApiKey={key}"
    )
    if tag: url += f"&Tag={tag}"
    return url

def scrape_serie(session, user_id, series_id, anime_name, key, no_tag=False):
    episodes = get_episodes(session, user_id, series_id)
    if not episodes: return []
    ep_list = []
    for ep in episodes:
        item_id = ep["Id"]
        season  = ep.get("ParentIndexNumber", 1)
        num_i   = ep.get("IndexNumber")
        num     = str(num_i) if num_i is not None else ep.get("Name", "?")
        if season and season > 1:
            num = f"S{season}E{num}"
        play_session = secrets.token_hex(16)
        if no_tag:
            msid, tag, aidx = item_id.replace("-", ""), "", None
        else:
            msid, tag, aidx = get_playback_info(session, user_id, item_id)
            msid = (msid or item_id).replace("-", "")
        ep_list.append({"id": new_uid(), "num": num,
                        "url": build_url(item_id, msid, key, play_session, tag, aidx)})
    return ep_list

def scrape_movie(session, user_id, item_id, key, no_tag=False):
    """Filmes não têm episódios — usa o próprio item como única entrada."""
    play_session = secrets.token_hex(16)
    if no_tag:
        msid, tag, aidx = item_id.replace("-", ""), "", None
    else:
        msid, tag, aidx = get_playback_info(session, user_id, item_id)
        msid = (msid or item_id).replace("-", "")
    return [{"id": new_uid(), "num": "1",
             "url": build_url(item_id, msid, key, play_session, tag, aidx)}]

# ── worker pool com throttle adaptativo ──────────────────────────────────────

class AdaptivePool:
    """ThreadPoolExecutor com semáforo adaptativo.
    Se a taxa de erro subir, reduz concorrência automaticamente.
    Se estabilizar, volta a subir até o máximo.
    """
    def __init__(self, max_workers):
        self.max_w    = max_workers
        self._sem     = threading.Semaphore(max_workers)
        self._lock    = threading.Lock()
        self._errors  = 0
        self._ok      = 0
        self._cur_w   = max_workers

    def acquire(self): self._sem.acquire()
    def release(self): self._sem.release()

    def report(self, success):
        with self._lock:
            if success:
                self._ok += 1
                # a cada 20 ok consecutivos, tenta aumentar 1 slot (até max)
                if self._ok % 20 == 0 and self._cur_w < self.max_w:
                    self._cur_w += 1
                    self._sem.release()
                self._errors = 0
            else:
                self._errors += 1
                self._ok = 0
                # a cada 3 erros seguidos, remove 1 slot (mínimo 2)
                if self._errors % 3 == 0 and self._cur_w > 2:
                    self._cur_w -= 1
                    self._sem.acquire()   # remove permissão extra

# ── modos ─────────────────────────────────────────────────────────────────────

def _run_parallel(session, user_id, args, pendentes, lib, label="all"):
    ok, fail, skip = 0, 0, 0
    falhos = []
    total  = len(pendentes)
    pool   = AdaptivePool(args.workers)

    # heartbeat
    _done_ev = threading.Event()
    def _hb():
        while not _done_ev.wait(20):
            with pool._lock:
                cur = pool._cur_w
            print(f"  … {ok+fail+skip}/{total} processados | workers ativos: {cur}", flush=True)
    threading.Thread(target=_hb, daemon=True).start()

    def _scrape_one(item):
        idx, serie = item
        name = serie["Name"]
        pool.acquire()
        try:
            if serie.get("Type") == "Movie":
                ep_list = scrape_movie(session, user_id, serie["Id"], args.api_key, args.no_tag)
            else:
                ep_list = scrape_serie(session, user_id, serie["Id"], name, args.api_key, args.no_tag)
            pool.report(True)
            return idx, serie, ep_list, None
        except Exception as e:
            pool.report(False)
            return idx, serie, [], str(e)
        finally:
            pool.release()

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(_scrape_one, (i, s)): i
                   for i, s in enumerate(pendentes, 1)}
        for fut in as_completed(futures):
            idx, serie, ep_list, err = fut.result()
            name = serie["Name"]
            year = f" ({serie['ProductionYear']})" if serie.get("ProductionYear") else ""
            tag  = f"[{idx:>4}/{total}]"
            if err:
                print(f"  {tag} {name}{year}  — erro: {err[:70]}")
                falhos.append(serie)
                fail += 1
            elif not ep_list:
                print(f"  {tag} {name}{year}  — sem eps")
                skip += 1
            else:
                anime = {"id": new_uid(), "name": name, "poster": "", "episodes": ep_list}
                n_bib = queue_anime(lib, anime)
                ok += 1
                print(f"  {tag} {name}{year}  → {len(ep_list)} eps  (biblioteca: {n_bib})")

    _done_ev.set()
    flush_library(lib)   # salva o que ficou no buffer

    # ── retry sequencial dos que falharam ────────────────────────────────────
    if falhos:
        print(f"\n  retentando {len(falhos)} que falharam…")
        for serie in falhos:
            name = serie["Name"]
            year = f" ({serie['ProductionYear']})" if serie.get("ProductionYear") else ""
            time.sleep(1 + random.random())
            try:
                if serie.get("Type") == "Movie":
                    ep_list = scrape_movie(session, user_id, serie["Id"], args.api_key, args.no_tag)
                else:
                    ep_list = scrape_serie(session, user_id, serie["Id"], name, args.api_key, args.no_tag)
                if ep_list:
                    anime = {"id": new_uid(), "name": name, "poster": "", "episodes": ep_list}
                    lib["animes"].append(anime)
                    flush_library(lib)
                    print(f"  ✓ {name}{year}  → {len(ep_list)} eps")
                    ok += 1; fail -= 1
                else:
                    print(f"  sem eps: {name}{year}")
            except Exception as e2:
                print(f"  ✗ {name}{year}  — {str(e2)[:70]}")

    total_eps = sum(len(a["episodes"]) for a in lib["animes"])
    print(f"\n  concluído: {ok} ok  {skip} sem eps  {fail} erro")
    print(f"  biblioteca: {len(lib['animes'])} animes / {total_eps} eps  →  {LIBRARY_FILE}")


def mode_scrap(session, user_id, args):
    print(f"  buscando: {args.anime}")
    data = api_get(session, f"/Users/{user_id}/Items",
                   searchTerm=args.anime,
                   IncludeItemTypes="Series",
                   Fields="Genres,Tags,OfficialRating,ProductionYear",
                   Recursive="true", Limit=50)
    results = data.get("Items", [])
    if not results:
        print("  nenhum resultado."); return

    if len(results) > 1:
        for i, r in enumerate(results):
            year = f" ({r['ProductionYear']})" if r.get("ProductionYear") else ""
            print(f"  [{i:>2}] {r['Name']}{year}  —  {r['Id']}")
        idx = int(input(f"\n  escolha (0-{len(results)-1}): ").strip())
        chosen = results[idx]
    else:
        chosen = results[0]

    lib = load_library()
    ep_list = scrape_serie(session, user_id, chosen["Id"], chosen["Name"], args.api_key, args.no_tag)
    if not ep_list:
        print("  sem episódios."); return
    lib["animes"] = [a for a in lib["animes"] if a["name"].lower() != chosen["Name"].lower()]
    lib["animes"].append({"id": new_uid(), "name": chosen["Name"], "poster": "", "episodes": ep_list})
    flush_library(lib)
    total = sum(len(a["episodes"]) for a in lib["animes"])
    print(f"  {chosen['Name']} → {len(ep_list)} eps  |  biblioteca: {len(lib['animes'])} animes / {total} eps")


def mode_all(session, user_id, args):
    all_series = get_all_series(session, user_id)
    print(f"  {len(all_series)} séries no servidor")

    clean, bloqueados = [], []
    for s in all_series:
        (bloqueados if is_nsfw(s) else clean).append(s)
    print(f"  {len(bloqueados)} removidos (NSFW)  →  {len(clean)} para scrap")
    if bloqueados:
        print("  — removidos (NSFW) —")
        for b in sorted(b["Name"] if isinstance(b, dict) else b for b in bloqueados):
            print(f"    {b}")

    lib       = load_library()
    done      = library_names(lib)
    pendentes = [s for s in clean if s["Name"].lower() not in done]
    print(f"  {len(done)} já na biblioteca  →  {len(pendentes)} novos\n")
    if not pendentes:
        print("  biblioteca já está completa."); return

    _run_parallel(session, user_id, args, pendentes, lib)


def mode_farm(session, user_id, args):
    all_series = get_all_series(session, user_id)
    clean      = [s for s in all_series if not is_nsfw(s)]
    lib        = load_library()
    done       = library_names(lib)
    novos      = [s for s in clean if s["Name"].lower() not in done]
    if not novos:
        print("  tudo já baixado."); return
    pick = random.sample(novos, min(args.farm_count, len(novos)))
    print(f"  sorteando {len(pick)} de {len(novos)} disponíveis\n")
    _run_parallel(session, user_id, args, pick, lib, label="farm")


def mode_update_library(session, user_id, args):
    lib = load_library()
    if not lib["animes"]:
        print("  biblioteca vazia."); return
    print(f"  {len(lib['animes'])} animes na biblioteca")
    ok = fail = 0
    for i, anime in enumerate(lib["animes"], 1):
        name = anime["name"]
        print(f"  [{i:>4}/{len(lib['animes'])}] {name}", end="  ", flush=True)
        updated = 0
        for ep in anime["episodes"]:
            item_id = item_id_from_url(ep["url"])
            if not item_id: continue
            play_session = secrets.token_hex(16)
            if args.no_tag:
                msid, tag, aidx = item_id, "", None
            else:
                msid, tag, aidx = get_playback_info(session, user_id, item_id)
                msid = (msid or item_id).replace("-", "")
            ep["url"] = build_url(item_id, msid, args.api_key, play_session, tag, aidx)
            updated += 1
        if updated:
            print(f"→ {updated} eps"); ok += 1
        else:
            print("— sem eps"); fail += 1
    flush_library(lib)
    print(f"\n  update: {ok} ok  {fail} falhou  →  {LIBRARY_FILE}")


def mode_fix_names():
    lib = load_library()
    if not lib["animes"]:
        print("  biblioteca vazia."); return
    ANILIST = "https://graphql.anilist.co"
    QUERY   = "query($q:String){Page(perPage:5){media(search:$q,type:ANIME){title{romaji english}}}}"
    fixed = skipped = 0
    print(f"  corrigindo {len(lib['animes'])} nomes via AniList…\n")
    for anime in lib["animes"]:
        old = anime["name"]
        try:
            resp = requests.post(ANILIST,
                json={"query": QUERY, "variables": {"q": old}},
                headers={"Content-Type": "application/json"}, timeout=10)
            resp.raise_for_status()
            media = resp.json().get("data", {}).get("Page", {}).get("media", [])
            if not media:
                skipped += 1; continue
            novo = media[0]["title"].get("english") or media[0]["title"].get("romaji") or old
            if novo.lower() != old.lower():
                print(f"  {old}  →  {novo}")
                anime["name"] = novo; fixed += 1
        except Exception as e:
            print(f"  erro ({old}): {e}"); skipped += 1
        time.sleep(0.5)
    flush_library(lib)
    print(f"\n  corrigidos: {fixed}  sem resultado: {skipped}")


def mode_scrap_legacy_jsons():
    files = [f for f in glob.glob("*.json") if f != LIBRARY_FILE]
    if not files:
        print("  nenhum JSON avulso."); return
    lib = load_library(); done = library_names(lib); added = 0
    for fname in sorted(files):
        try:
            with open(fname, encoding="utf-8") as f: data = json.load(f)
        except Exception: continue
        for anime in data.get("animes", []):
            if anime["name"].lower() in done:
                print(f"  duplicado: {anime['name']}"); continue
            lib["animes"].append(anime); done.add(anime["name"].lower())
            print(f"  + {anime['name']}  ({len(anime['episodes'])} eps)"); added += 1
    flush_library(lib)
    total = sum(len(a["episodes"]) for a in lib["animes"])
    print(f"\n  importados: {added}  |  biblioteca: {len(lib['animes'])} animes / {total} eps")

# ── GitHub sync ───────────────────────────────────────────────────────────────

GITHUB_REPO   = "chaoshubopensource/bibliotecadeanimes"
GITHUB_PATH   = "biblioteca_completa.json"
GITHUB_BRANCH = "main"

def pull_from_github():
    """Baixa a biblioteca atual do GitHub antes de rodar (mantém estado incremental no Render)."""
    url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/{GITHUB_PATH}"
    try:
        r = requests.get(url, timeout=60)
        if r.ok:
            with open(LIBRARY_FILE, "wb") as f:
                f.write(r.content)
            kb = len(r.content) // 1024
            print(f"  ✓ biblioteca baixada do GitHub ({kb} KB)")
        else:
            print(f"  ⚠ não consegui baixar do GitHub ({r.status_code}) — começa do zero")
    except Exception as e:
        print(f"  ⚠ erro ao baixar do GitHub: {e} — começa do zero")

def push_to_github(token: str):
    """Faz upload do biblioteca_completa.json direto via GitHub API (sem git)."""
    if not token:
        print("  ⚠ GITHUB_TOKEN não definido — push ignorado.")
        return

    api = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_PATH}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # 1. pega o SHA atual do arquivo (necessário para o PUT)
    sha = None
    try:
        r = requests.get(api, headers=headers, timeout=15)
        if r.ok:
            sha = r.json().get("sha")
    except Exception as e:
        print(f"  ⚠ não consegui ler SHA atual: {e}")

    # 2. lê e codifica o arquivo
    with open(LIBRARY_FILE, "rb") as f:
        conteudo_b64 = _b64.b64encode(f.read()).decode()

    agora = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body  = {
        "message": f"auto: atualiza biblioteca ({agora})",
        "content": conteudo_b64,
        "branch":  GITHUB_BRANCH,
    }
    if sha:
        body["sha"] = sha

    # 3. faz o PUT
    try:
        r = requests.put(api, headers=headers, json=body, timeout=60)
        if r.ok:
            print(f"  ✓ biblioteca enviada ao GitHub ({GITHUB_REPO}/{GITHUB_PATH})")
        else:
            print(f"  ✗ erro no push: {r.status_code} — {r.text[:200]}")
    except Exception as e:
        print(f"  ✗ falha no push: {e}")

# ── main ──────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--api-key",    required=True)
    p.add_argument("--user-id",    default=None)
    p.add_argument("--anime",      default=None)
    p.add_argument("--no-tag",     action="store_true")
    p.add_argument("--farm-count", type=int, default=50)
    p.add_argument("--workers",    type=int, default=8)
    p.add_argument("--github-token", default=os.environ.get("GITHUB_TOKEN"),
                   help="PAT do GitHub para push automático (ou env GITHUB_TOKEN)")
    p.add_argument("--pull-github", action="store_true",
                   help="baixa a biblioteca atual do GitHub antes de rodar (para uso no Render)")
    p.add_argument("--mode",
        choices=["scrap","all","farm","update-library","import-jsons","fix-names"],
        default=None)
    args = p.parse_args()

    if not args.mode:
        print("\n  AniStream Scraper")
        print("  ──────────────────────────────────────────────────────")
        print("  [1] Scrap          — busca um anime e adiciona")
        print("  [2] Baixar tudo    — scrapa TODOS (retoma de onde parou)")
        print("  [3] Farm           — sorteia N aleatórios ainda não baixados")
        print("  [4] Atualizar bib  — renova todos os links")
        print("  [5] Importar JSONs — absorve JSONs avulsos da pasta")
        print("  [6] Corrigir nomes — corrige títulos via AniList")
        choice = input("\n  escolha (1-6): ").strip()
        args.mode = {"1":"scrap","2":"all","3":"farm","4":"update-library",
                     "5":"import-jsons","6":"fix-names"}.get(choice, "scrap")

    if args.mode == "import-jsons":
        mode_scrap_legacy_jsons(); sys.exit(0)
    if args.mode == "fix-names":
        mode_fix_names(); sys.exit(0)

    if args.mode == "scrap" and not args.anime:
        args.anime = input("  nome do anime: ").strip()
        if not args.anime:
            print("  nome vazio."); sys.exit(1)

    # no Render o disco é efêmero — baixa o estado atual do GitHub antes de rodar
    if args.pull_github:
        print("  baixando biblioteca do GitHub…")
        pull_from_github()

    s = make_session(args.api_key)
    print("  autenticando...")
    user_id = args.user_id or get_user_id(s)
    if not user_id:
        print("  não consegui resolver o UserId. passe com --user-id."); sys.exit(1)
    print(f"  userId: {user_id}\n")

    if   args.mode == "scrap":          mode_scrap(s, user_id, args)
    elif args.mode == "all":            mode_all(s, user_id, args)
    elif args.mode == "farm":           mode_farm(s, user_id, args)
    elif args.mode == "update-library": mode_update_library(s, user_id, args)

    # push automático ao GitHub se token disponível
    if args.github_token and args.mode in ("all", "farm", "update-library", "scrap"):
        print("\n  enviando para o GitHub…")
        push_to_github(args.github_token)

if __name__ == "__main__":
    main()
