#!/usr/bin/env python3
"""
anistream_scraper.py
Extrai links HLS e metadados de uma instância Jellyfin (AniStream) e gera biblioteca_completa.json.
Modos: all | update-library | patch-logos | scrap | farm | fix-names | import-jsons
"""

import argparse
import base64
import glob
import json
import os
import random
import secrets
import sys
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURAÇÕES & CONSTANTES
# ──────────────────────────────────────────────────────────────────────────────
BASE_URL = "https://app.anistream.biz"
LIBRARY_PATH = "biblioteca_completa.json"
REQUEST_TIMEOUT = 30  # segundos (aumentado — servidor pode demorar mais quando throttled)
PAGE_SIZE = 500       # itens por página nas listagens do Jellyfin

# DeviceId fixo — o servidor Jellyfin usa para rastrear sessões; não alterar.
DEVICE_ID = "a9d0ebde-e77e-48d6-a6ee-cbfa9c991c1f"

# User-Agent escolhido aleatoriamente uma vez por sessão — varia entre runs para não
# ter fingerprint fixo. Cloudflare correlaciona UA + frequência.
_SESSION_UA = random.choice([
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
])

# Parâmetros fixos da URL HLS (main.m3u8).
# AudioCodec=copy: browsers não decodificam AC3/EAC3 via MSE; o frontend converte
# copy→aac em fixUrl() e o Jellyfin transcodifica quando o perfil do device pede AAC.
STREAM_BASE = {"AudioCodec": "copy"}

# Idiomas aceitos como português. O Jellyfin costuma usar ISO 639-2 ("por"), não "pt".
PT_LANGS = {"pt", "por", "pt-br", "ptbr", "pob"}

# Limite de falhas de AUTENTICAÇÃO (401/403) seguidas no update-library antes de abortar.
# Falhas legítimas (episódio removido, sem áudio etc.) não contam.
MAX_FALHAS_SEGUIDAS = 50

# Status HTTP transitórios (comuns atrás do Cloudflare) que merecem retry.
RETRY_STATUS = {429, 502, 503, 504}

# Estado interno: indica se a última resolução de episódio falhou por 401/403.
_STATE = {"auth_fail": False}

# 🔒 SEGURANÇA: as listas NSFW vêm de variáveis de ambiente (base64) e são
# decodificadas apenas em memória durante a execução.
NSFW_GENRES_B64 = os.getenv("NSFW_GENRES_B64", "")
NSFW_TAGS_B64 = os.getenv("NSFW_TAGS_B64", "")
NSFW_KEYWORDS_B64 = os.getenv("NSFW_KEYWORDS_B64", "")
NSFW_TITLE_BLOCKS_B64 = os.getenv("NSFW_TITLE_BLOCKS_B64", "")
NSFW_WHITELIST_SUBS_B64 = os.getenv("NSFW_WHITELIST_SUBS_B64", "")


def _b64_to_list(raw: str) -> List[str]:
    """Decodifica string base64 em lista lowercase, ignora falhas e linhas em branco.

    Linhas em branco precisam ser descartadas: "" in texto é sempre True, então um
    item vazio faria tudo ser bloqueado (keywords) ou liberado (whitelist).
    """
    if not raw: return []
    try:
        linhas = base64.b64decode(raw).decode("utf-8").splitlines()
        return [p for p in (l.lower().strip() for l in linhas) if p]
    except Exception:
        return []


FILTERS = {
    "genres": _b64_to_list(NSFW_GENRES_B64),
    "tags": _b64_to_list(NSFW_TAGS_B64),
    "keywords": _b64_to_list(NSFW_KEYWORDS_B64),
    "title_blocks": _b64_to_list(NSFW_TITLE_BLOCKS_B64),
    "whitelist_subs": _b64_to_list(NSFW_WHITELIST_SUBS_B64),
}


def _human_sleep(min_s: float = 4.0, max_s: float = 12.0):
    """Pausa aleatória humanizada entre requests — evita fingerprint de bot pelo Cloudflare WAF."""
    time.sleep(random.uniform(min_s, max_s))


def _headers(api_key: str) -> Dict[str, str]:
    """Headers que imitam um browser Chrome real. O Cloudflare inspeciona o conjunto completo."""
    return {
        "Authorization": (
            f'MediaBrowser Token="{api_key}", Client="Jellyfin Web", '
            f'Device="Chrome", DeviceId="{DEVICE_ID}", Version="10.9.11"'
        ),
        "X-Emby-Token": api_key,
        "User-Agent": _SESSION_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": f"{BASE_URL}/",
        "Origin": BASE_URL,
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "sec-ch-ua": '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }


def _api_get(endpoint: str, params: Optional[Dict] = None, _no_sleep: bool = False, **kwargs) -> requests.Response:
    """GET com retry exponencial + delay humanizado após cada request bem-sucedido.
    _no_sleep=True desativa o delay (usado em listagens de paginação onde já há pausa fora)."""
    url = f"{BASE_URL}{endpoint}"
    if params:
        url += f"?{urlencode(params)}"
    api_key = kwargs.pop("apikey", None) or os.environ["ANISTREAM_API_KEY"]
    last_exc = None
    for attempt in range(5):
        if attempt:
            # backoff exponencial com jitter para não criar picos de tráfego
            wait = (2 ** attempt) + random.uniform(0, 3)
            print(f"[RETRY] tentativa {attempt + 1}/5 após {wait:.1f}s ({last_exc})")
            time.sleep(wait)
        try:
            resp = requests.get(url, headers=_headers(api_key), timeout=REQUEST_TIMEOUT, stream=False)
        except (requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.Timeout) as exc:
            last_exc = exc
            continue
        if resp.status_code == 401:
            # Fallback: alguns proxies/WAFs removem os headers de auth; tenta a chave via query string.
            try:
                sep = "&" if "?" in url else "?"
                resp2 = requests.get(f"{url}{sep}{urlencode({'api_key': api_key})}",
                                     headers=_headers(api_key), timeout=REQUEST_TIMEOUT, stream=False)
                if resp2.status_code != 401:
                    if not _no_sleep:
                        _human_sleep()
                    return resp2
            except requests.exceptions.RequestException:
                pass
        if resp.status_code == 429:
            # Rate-limited: espera mais antes de tentar de novo
            retry_after = int(resp.headers.get("Retry-After", 60))
            wait = max(retry_after, 30) + random.uniform(5, 15)
            print(f"[RATE-LIMIT] 429 recebido. Aguardando {wait:.0f}s antes de continuar...")
            time.sleep(wait)
            last_exc = "HTTP 429"
            continue
        if resp.status_code in RETRY_STATUS and attempt < 4:
            last_exc = f"HTTP {resp.status_code}"
            continue
        # Cloudflare às vezes retorna 200 com HTML (captcha / JS challenge) em vez de JSON.
        # Detecta pelo Content-Type e trata como erro retryável com backoff longo.
        if resp.status_code == 200:
            ct = resp.headers.get("Content-Type", "")
            if "text/html" in ct:
                wait_cf = 30 + random.uniform(10, 30)
                print(f"[CLOUDFLARE] Resposta HTML recebida (bloqueio/challenge). "
                      f"Aguardando {wait_cf:.0f}s antes de tentar novamente...")
                time.sleep(wait_cf)
                last_exc = "200 HTML (Cloudflare block)"
                continue

        # Pausa humanizada após cada request bem-sucedido (não em retries — esses já têm backoff)
        if not _no_sleep and attempt == 0 and resp.status_code < 400:
            _human_sleep()
        return resp
    raise requests.exceptions.ConnectionError(f"falhou após 5 tentativas: {last_exc}")


def _api_post(endpoint: str, json_payload: Dict, **kwargs) -> requests.Response:
    url = f"{BASE_URL}{endpoint}"
    api_key = kwargs.pop("apikey", None) or os.environ["ANISTREAM_API_KEY"]
    return requests.post(url, headers=_headers(api_key), json=json_payload, timeout=REQUEST_TIMEOUT)


def _check(resp: requests.Response, what: str) -> None:
    """raise_for_status com diagnóstico: status, servidor e trecho do corpo (nunca imprime a chave)."""
    if resp.status_code < 400:
        return
    h = resp.headers
    via_cf = " | cf-ray presente (resposta via Cloudflare)" if h.get("CF-RAY") else ""
    print(f"[ERROR] HTTP {resp.status_code} em {what} | server={h.get('Server', '?')} | "
          f"content-type={h.get('Content-Type', '?')}{via_cf}")
    print(f"[ERROR] corpo: {resp.text[:300]!r}")
    if resp.status_code in (401, 403):
        print("[HINT] Confira ANISTREAM_API_KEY/ANISTREAM_USER_ID, as permissões desse usuário no Jellyfin "
              "ou bloqueio por IP/WAF (corpo em HTML + cf-ray indica bloqueio do Cloudflare).")
    resp.raise_for_status()


def _safe_json(resp: requests.Response, what: str) -> Dict:
    """Parseia JSON da resposta; lança RequestException com mensagem clara se o corpo for HTML
    (Cloudflare block/captcha que escapou da detecção em _api_get)."""
    try:
        return resp.json()
    except ValueError:
        ct = resp.headers.get("Content-Type", "")
        body = resp.text[:400]
        if "text/html" in ct or body.strip().startswith("<"):
            raise requests.exceptions.RequestException(
                f"[CLOUDFLARE] Resposta HTML em vez de JSON em '{what}' "
                f"(status={resp.status_code}). O servidor está bloqueando temporariamente. "
                "O próximo run deve funcionar após o cooldown."
            )
        raise requests.exceptions.RequestException(
            f"JSON inválido em '{what}' (status={resp.status_code}): {body[:200]}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# FUNÇÕES DE MÍNIMA UTILIDADE
# ──────────────────────────────────────────────────────────────────────────────
def load_library(path: str = LIBRARY_PATH) -> Dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"app": "AniLib", "animes": []}


def save_library(lib: Dict, path: str = LIBRARY_PATH):
    # Escrita atômica: se o processo morrer no meio, o arquivo anterior permanece íntegro.
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(lib, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _exists_in_lib(anime_id: str, lib: Dict) -> bool:
    return any(a["id"] == anime_id for a in lib["animes"])


def _find_anime_by_id(anime_id: str, lib: Dict) -> Optional[Dict]:
    return next((a for a in lib["animes"] if a["id"] == anime_id), None)


def _norm(text: Optional[str]) -> str:
    """Normaliza nome para comparação (case-insensitive, espaços colapsados)."""
    return " ".join((text or "").casefold().split())


def _clean_id(raw_id: str) -> str:
    """Remove prefixo 'id-' legado que o scraper antigo adicionava aos IDs do Jellyfin.
    (Sem str.removeprefix para funcionar em Python < 3.9.)"""
    if raw_id and raw_id.startswith("id-"):
        return raw_id[3:]
    return raw_id


# ──────────────────────────────────────────────────────────────────────────────
# EXTRAÇÃO DE METADADOS E STREAMS
# ──────────────────────────────────────────────────────────────────────────────
def get_all_series(user_id: str, api_key: str) -> List[Dict]:
    """Lista Series/Movies (paginado). Cada item: {"id", "name", "metadata": <item cru do Jellyfin>}."""
    items: List[Dict] = []
    start = 0
    while True:
        params = {
            "IncludeItemTypes": "Series,Movie",
            "Recursive": "true",
            "Fields": "Genres,Tags,OfficialRating,ProductionYear,Overview,ImageTags,ChildCount,RecursiveItemCount",
            "SortBy": "SortName",  # ordem estável, necessária para paginar sem duplicar
            "StartIndex": start,
            "Limit": PAGE_SIZE,
        }
        resp = _api_get(f"/Users/{user_id}/Items", params=params, apikey=api_key)
        _check(resp, "listagem de séries (/Users/<id>/Items)")
        data = _safe_json(resp, "listagem de séries")
        page = data.get("Items", [])
        items.extend(page)
        start += len(page)
        if len(page) < PAGE_SIZE or start >= data.get("TotalRecordCount", start):
            break
    return [{"id": i["Id"], "name": i.get("Name", ""), "metadata": i} for i in items]


def _ep_sort_key(ep: Dict) -> Tuple[int, int]:
    season = ep.get("ParentIndexNumber")
    if season == 0:
        season = 10 ** 6  # especiais por último, para não deslocar a numeração
    elif season is None:
        season = 1
    return (season, ep.get("IndexNumber") or 0)


def get_episodes(serie_id: str, user_id: str, api_key: str) -> List[Dict]:
    """Lista os episódios de uma série (recursivo — filhos diretos de uma Series são Seasons)."""
    episodes: List[Dict] = []
    start = 0
    while True:
        params = {
            "ParentId": serie_id,
            "Recursive": "true",
            "IncludeItemTypes": "Episode",
            "SortBy": "ParentIndexNumber,IndexNumber,SortName",
            "StartIndex": start,
            "Limit": PAGE_SIZE,
        }
        resp = _api_get(f"/Users/{user_id}/Items", params=params, apikey=api_key)
        _check(resp, f"listagem de episódios de {serie_id}")
        data = _safe_json(resp, f"listagem de episódios de {serie_id}")
        page = data.get("Items", [])
        episodes.extend(page)
        start += len(page)
        if len(page) < PAGE_SIZE or start >= data.get("TotalRecordCount", start):
            break
    episodes.sort(key=_ep_sort_key)
    return episodes


def _is_pt(stream: Dict) -> bool:
    lang = (stream.get("Language") or "").lower().replace("_", "-").strip()
    if lang in PT_LANGS or lang.startswith("pt-"):
        return True
    if lang in ("", "und"):  # sem idioma marcado: tenta pelo título da faixa
        title = f"{stream.get('Title') or ''} {stream.get('DisplayTitle') or ''}".lower()
        return "portugu" in title or "pt-br" in title or "ptbr" in title
    return False


def _detect_pt_streams(item_id: str, api_key: str, user_id: str = "") -> Optional[Tuple[int, int]]:
    """Retorna (audio_idx, sub_idx) — -1 quando a faixa não existe — ou None em caso de falha.
    Printa erro explícito em caso de falha HTTP/autenticação/rede."""
    try:
        endpoint = (f"/Users/{user_id}/Items/{item_id}?Fields=MediaStreams"
                    if user_id else f"/Items/{item_id}?Fields=MediaStreams")
        resp = _api_get(endpoint, apikey=api_key)
    except requests.RequestException as e:
        print(f"[ERROR] Falha de rede ao buscar streams do item {item_id}: {e}")
        return None

    if resp.status_code in (401, 403):
        _STATE["auth_fail"] = True
        print(f"[ERROR] Credenciais inválidas ou permissão negada para /Items/{item_id}. Status: {resp.status_code}")
        return None
    elif resp.status_code != 200:
        print(f"[ERROR] Falha ao buscar streams do item {item_id}. Status: {resp.status_code} - {resp.text[:200]}")
        return None

    try:
        data = resp.json()
        sources = data.get("MediaSources") or [{}]
        streams = sources[0].get("MediaStreams") or data.get("MediaStreams") or []

        pt_audio_idx = None
        first_audio_idx = None
        pt_sub_idx = None

        for i, stream in enumerate(streams):
            stype = (stream.get("Type") or "").lower()
            # AudioStreamIndex/SubtitleStreamIndex do Jellyfin = campo "Index", não a posição na lista
            idx = stream.get("Index", i)

            if stype == "audio":
                if first_audio_idx is None:
                    first_audio_idx = idx
                if pt_audio_idx is None and _is_pt(stream):
                    pt_audio_idx = idx
            elif stype == "subtitle":
                # só legendas de texto (convertíveis p/ VTT) e não "forced"
                if pt_sub_idx is None and _is_pt(stream) and not stream.get("IsForced") \
                        and stream.get("IsTextSubtitleStream", True):
                    pt_sub_idx = idx

        if pt_audio_idx is None:
            if first_audio_idx is None:
                print(f"[WARN] Nenhuma faixa de áudio encontrada para {item_id}.")
            else:
                print(f"[WARN] Nenhum áudio PT-BR detectado para {item_id}. Usando primeiro áudio disponível.")
                pt_audio_idx = first_audio_idx

        return (pt_audio_idx if pt_audio_idx is not None else -1, pt_sub_idx if pt_sub_idx is not None else -1)

    except Exception as e:
        print(f"[ERROR] Falha interna ao interpretar MediaStreams de {item_id}: {e}")
        return None


def _playback_info_post(item_id: str, api_key: str, audio_stream_index: int) -> Optional[Dict]:
    payload = {
        "DeviceProfile": {
            "Name": "AniLib Web Client",
            "MaxStreamingBitrate": 120000000,
            "TranscodingProtocols": ["hls"],
            "DirectPlayProfiles": [{"Container": "ts,mpegts,m3u8,aac", "AudioCodec": "aac"}]
        },
        "AudioStreamIndex": audio_stream_index,
        "SubtitleStreamIndex": -1,
        "OpenSubtitlesFormat": 0,
        "IsPlayback": True
    }
    resp = _api_post(f"/Items/{item_id}/PlaybackInfo", json_payload=payload, apikey=api_key)
    _check(resp, f"PlaybackInfo de {item_id}")
    data = resp.json()

    media_sources = data.get("MediaSources", [])
    if not media_sources:
        print(f"[WARN] PlaybackInfo retornou vazio para {item_id}")
        return None

    source = media_sources[0]
    return {
        "media_source_id": source.get("Id", ""),
        "etag": source.get("Etag", ""),
        "streams": source.get("MediaStreams", [])
    }


def build_hls_url(item_id: str, playback: Dict, api_key: str, audio_idx: int) -> str:
    params = {
        **STREAM_BASE,
        "DeviceId": DEVICE_ID,
        "MediaSourceId": playback["media_source_id"],
        "AudioStreamIndex": audio_idx,
        "PlaySessionId": secrets.token_hex(16),
        "ApiKey": api_key,
    }
    return f"{BASE_URL}/videos/{item_id}/main.m3u8?{urlencode(params)}"


def build_sub_url(item_id: str, media_source_id: str, sub_idx: int, api_key: str) -> str:
    return f"{BASE_URL}/Videos/{item_id}/{media_source_id}/Subtitles/{sub_idx}/0/Stream.vtt?{urlencode({'ApiKey': api_key})}"


def _image_url(serie: Dict, kind: str, size_qs: str) -> str:
    """Monta URL de imagem a partir de ImageTags. Aceita o item cru do Jellyfin
    ou o wrapper {"id", "name", "metadata"} retornado por get_all_series."""
    meta = serie.get("metadata", serie)
    tag = (meta.get("ImageTags") or {}).get(kind)
    if not tag:
        return ""
    item_id = _clean_id(serie.get("id") or meta.get("Id") or "")
    return f"{BASE_URL}/Items/{item_id}/Images/{kind}?tag={tag}&{size_qs}"


def logo_url(serie: Dict) -> str:
    """Gera URL de logo sem depender de ImageTags.Logo (nem sempre vem na listagem).
    O servidor retorna 404 silencioso se a logo não existir — o frontend trata com onerror."""
    meta = serie.get("metadata", serie)
    item_id = serie.get("id") or meta.get("Id") or ""
    item_id = _clean_id(item_id)  # remove prefixo legado do scraper antigo
    if not item_id:
        return ""
    # usa tag se disponível (cache-busting), senão manda sem — funciona do mesmo jeito
    tag = (meta.get("ImageTags") or {}).get("Logo")
    qs = f"tag={tag}&" if tag else ""
    return f"{BASE_URL}/Items/{item_id}/Images/Logo?{qs}fillWidth=1344&fillHeight=256&quality=75"


def poster_url(serie: Dict) -> str:
    return _image_url(serie, "Primary", "fillWidth=400&quality=85")


def _resolve_episode(ep_id: str, api_key: str, user_id: str = "") -> Optional[Dict]:
    """Detecta streams PT, consulta PlaybackInfo e monta {"url", "sub"}. None se falhar."""
    _STATE["auth_fail"] = False
    ep_id = _clean_id(ep_id)
    streams = _detect_pt_streams(ep_id, api_key, user_id)
    if streams is None:
        return None
    audio_idx, sub_idx = streams
    if audio_idx < 0:
        print(f"[WARN] Sem faixa de áudio utilizável em {ep_id}. Pulando.")
        return None

    try:
        playback = _playback_info_post(ep_id, api_key, audio_idx)
    except (requests.RequestException, ValueError) as e:
        err_resp = getattr(e, "response", None)
        if err_resp is not None and err_resp.status_code in (401, 403):
            _STATE["auth_fail"] = True
        print(f"[ERROR] PlaybackInfo falhou para {ep_id}: {e}")
        return None
    if not playback:
        return None

    sub = build_sub_url(ep_id, playback["media_source_id"], sub_idx, api_key) if sub_idx >= 0 else ""
    return {"url": build_hls_url(ep_id, playback, api_key, audio_idx), "sub": sub}


# ──────────────────────────────────────────────────────────────────────────────
# FILTROS & VALIDAÇÃO
# ──────────────────────────────────────────────────────────────────────────────
def is_nsfw(serie_meta: Dict) -> bool:
    # "or" cobre campos presentes mas com valor null no JSON do Jellyfin
    name_lower = (serie_meta.get("Name") or "").lower()
    genres = [g.lower() for g in (serie_meta.get("Genres") or [])]
    tags = [t.lower() for t in (serie_meta.get("Tags") or [])]

    # Whitelist: compara com o título da série
    whitelist_subs = FILTERS.get("whitelist_subs", [])
    # Se o título contiver um termo da whitelist, ignora filtro
    if whitelist_subs and any(s in name_lower for s in whitelist_subs):
        return False

    # Verifica gêneros/tags
    if any(g in genres for g in FILTERS["genres"]): return True
    if any(t in tags for t in FILTERS["tags"]): return True
    if any(k in name_lower for k in FILTERS["keywords"]): return True
    if any(block in name_lower for block in FILTERS["title_blocks"]): return True

    return False


# ──────────────────────────────────────────────────────────────────────────────
# MODOS DE OPERAÇÃO
# ──────────────────────────────────────────────────────────────────────────────
def _is_full_id(raw_id: Optional[str]) -> bool:
    """IDs do Jellyfin têm 32 caracteres hex. Menos que isso = ID truncado (biblioteca antiga)."""
    return bool(raw_id) and len(raw_id) >= 32


def _find_full(short: str, candidates: List[str]) -> Optional[str]:
    """Acha o ID completo de um ID truncado (prefixo ou sufixo). Só aceita se o match for único."""
    s = (short or "").lower()
    if not s:
        return None
    for test in (str.startswith, str.endswith):
        hits = [c for c in candidates if test(c.lower(), s)]
        if len(hits) == 1:
            return hits[0]
    return None


def _ensure_full_ids(lib: Dict, user_id: str, api_key: str) -> int:
    """Repara IDs truncados (anime e episódios) consultando o Jellyfin. Só faz requisições se
    existir algum ID curto. Retorna quantos IDs foram corrigidos."""
    def _short(i: Optional[str]) -> bool:
        return not _is_full_id(_clean_id(i or ""))

    precisa = any(_short(a.get("id")) or any(_short(e.get("id")) for e in a.get("episodes", []))
                  for a in lib["animes"])
    if not precisa:
        return 0
    if not user_id:
        print("[WARN] Biblioteca tem IDs truncados, mas ANISTREAM_USER_ID não foi informado: "
              "não é possível repará-los.")
        return 0

    print("[INFO] IDs truncados detectados na biblioteca. Reconstruindo IDs completos via Jellyfin...")
    all_series = get_all_series(user_id, api_key)
    series_ids = [s["id"] for s in all_series]
    by_name: Dict[str, List[str]] = {}
    for s in all_series:
        by_name.setdefault(_norm(s["name"]), []).append(s["id"])

    fixed = unresolved = 0
    for anime in lib["animes"]:
        aid = _clean_id(anime.get("id") or "")
        if _short(aid):
            full = _find_full(aid, series_ids)
            if not full:
                nomes = by_name.get(_norm(anime.get("name")), [])
                full = nomes[0] if len(nomes) == 1 else None
            if not full:
                unresolved += 1
                continue
            anime["id"] = full
            fixed += 1
        else:
            full = aid

        if any(_short(e.get("id")) for e in anime.get("episodes", [])):
            try:
                eps = get_episodes(full, user_id, api_key)
            except requests.RequestException as e:
                print(f"[WARN] Não foi possível listar episódios de '{anime.get('name')}' para reparar IDs: {e}")
                continue
            cands = [e["Id"] for e in eps] + [full]  # filme: o episódio é o próprio item
            for ep in anime.get("episodes", []):
                if _short(ep.get("id")):
                    f = _find_full(_clean_id(ep.get("id") or ""), cands)
                    if f:
                        ep["id"] = f
                        fixed += 1
    print(f"[INFO] IDs reparados: {fixed} | animes sem correspondência: {unresolved}.")
    return fixed


def scrape_anime(serie_id: str, serie_data: Dict, api_key: str, user_id: str) -> Optional[Dict]:
    meta = serie_data["metadata"]
    nome = meta.get("Name", "Unknown")

    if is_nsfw(meta):
        print(f"[SKIP] Conteúdo NSFW filtrado: {nome}")
        return None

    # Movie é o próprio "episódio"; Series precisa listar os Episodes (recursivo).
    if meta.get("Type") == "Movie":
        episodes_raw = [meta]
    else:
        try:
            episodes_raw = get_episodes(serie_id, user_id, api_key)
        except requests.RequestException as e:
            print(f"[ERROR] Falha ao listar episódios de {nome}: {e}")
            return None

    episodes = []
    for i, ep in enumerate(episodes_raw):
        resolved = _resolve_episode(ep["Id"], api_key, user_id)
        if not resolved:
            continue
        episodes.append({
            "id": ep["Id"],
            "num": str(i + 1),
            "url": resolved["url"],
            "sub": resolved["sub"]
        })

    if not episodes:
        # não retorna anime vazio: assim ele não entra na biblioteca e é tentado de novo na próxima execução
        print(f"[WARN] Nenhum episódio utilizável em {nome}. Não adicionado.")
        return None

    return {
        "id": serie_id,
        "name": nome,
        "poster": poster_url(serie_data),
        "logo_url": logo_url(serie_data),
        "year": meta.get("ProductionYear", ""),
        "overview": meta.get("Overview", ""),
        "episodes": episodes
    }


def mode_sync(api_key: str, user_id: str, max_runtime_min: int = 0):
    """Modo inteligente: adiciona animes/episódios novos sem varrer tudo.
    Usa RecursiveItemCount da listagem (zero requests extras) pra detectar mudança.
    Só chama get_episodes quando o contador diverge do que está salvo.

    Suporta execuções parciais: salva checkpoint em lib["_sync_checkpoint"] e retoma
    de onde parou no próximo run. max_runtime_min=0 significa sem limite de tempo.
    """
    _run_start = time.time()

    def _tempo_ok() -> bool:
        """False quando o tempo limite foi ultrapassado."""
        if not max_runtime_min:
            return True
        elapsed = (time.time() - _run_start) / 60
        return elapsed < max_runtime_min

    print("[INFO] Iniciando SYNC (novos animes + episódios novos)...")
    lib = load_library()

    # Checkpoint: IDs já processados neste ciclo. Quando varrer tudo, o checkpoint é apagado.
    checkpoint: set = set(lib.get("_sync_checkpoint", {}).get("done", []))
    if checkpoint:
        print(f"[CHECKPOINT] Retomando ciclo anterior — {len(checkpoint)} animes já processados.")

    _ensure_full_ids(lib, user_id, api_key)
    save_library(lib)

    all_series = get_all_series(user_id, api_key)
    lib_by_id: Dict[str, Dict] = {_clean_id(a["id"]): a for a in lib["animes"]}

    novos = atualizados = sem_mudanca = 0
    parou_por_tempo = False

    try:
        for s in all_series:
            if not _tempo_ok():
                parou_por_tempo = True
                print(f"[CHECKPOINT] Limite de {max_runtime_min}min atingido. "
                      f"Salvando progresso — próximo run continua daqui.")
                break

            sid = s["id"]

            # Pula animes já processados neste ciclo
            if sid in checkpoint:
                sem_mudanca += 1
                continue

            anime_salvo = lib_by_id.get(sid)

            if anime_salvo is None:
                # ── anime novo ──────────────────────────────────────────────
                if is_nsfw(s["metadata"]):
                    checkpoint.add(sid)
                    continue
                result = scrape_anime(sid, s, api_key, user_id)
                if result:
                    lib["animes"].append(result)
                    lib_by_id[sid] = result
                    novos += 1
                    print(f"[NEW] {result['name']} ({len(result['episodes'])} eps)")
                checkpoint.add(sid)
                lib["_sync_checkpoint"] = {"done": list(checkpoint)}
                if novos % 5 == 0 or novos == 1:
                    save_library(lib)
                continue

            # ── anime existente: compara contagem via RecursiveItemCount ─────
            meta = s["metadata"]
            count_server = meta.get("RecursiveItemCount") or meta.get("ChildCount") or 0
            count_local = len(anime_salvo.get("episodes", []))

            if count_server and count_server <= count_local:
                checkpoint.add(sid)
                sem_mudanca += 1
                continue

            # contagem divergiu: busca eps reais pra saber quais são novos
            try:
                eps_jellyfin = get_episodes(sid, user_id, api_key)
            except requests.RequestException as e:
                print(f"[WARN] Falha ao listar eps de '{anime_salvo.get('name')}': {e}")
                # não marca como done — tenta de novo no próximo run
                continue

            eps_salvas = {_clean_id(e["id"]) for e in anime_salvo.get("episodes", [])}
            eps_novos = [ep for ep in eps_jellyfin if ep["Id"] not in eps_salvas]

            if not eps_novos:
                checkpoint.add(sid)
                sem_mudanca += 1
                continue

            print(f"[UPDATE] {anime_salvo.get('name')}: {len(eps_novos)} ep(s) novo(s)")
            adicionados = 0
            for ep in eps_novos:
                if not _tempo_ok():
                    parou_por_tempo = True
                    break
                resolved = _resolve_episode(ep["Id"], api_key, user_id)
                if resolved:
                    num = str(count_local + adicionados + 1)
                    anime_salvo.setdefault("episodes", []).append({
                        "id": ep["Id"],
                        "num": num,
                        "url": resolved["url"],
                        "sub": resolved["sub"],
                    })
                    adicionados += 1

            if not anime_salvo.get("logo_url"):
                anime_salvo["logo_url"] = logo_url(s)
            if not anime_salvo.get("poster"):
                anime_salvo["poster"] = poster_url(s)

            if adicionados:
                atualizados += 1

            if not parou_por_tempo:
                checkpoint.add(sid)

            lib["_sync_checkpoint"] = {"done": list(checkpoint)}
            save_library(lib)

            if parou_por_tempo:
                break

        else:
            # loop terminou sem break = varreu tudo neste ciclo → limpa checkpoint
            if "_sync_checkpoint" in lib:
                del lib["_sync_checkpoint"]
            print("[CHECKPOINT] Ciclo completo. Checkpoint apagado.")

    finally:
        lib["_sync_checkpoint"] = {"done": list(checkpoint)}
        save_library(lib)
        # Se ciclo terminou limpo, já foi apagado antes do finally — garante limpeza
        if not parou_por_tempo and "_sync_checkpoint" in lib:
            del lib["_sync_checkpoint"]
            save_library(lib)

    status = "PAUSADO (continua no próximo run)" if parou_por_tempo else "CONCLUÍDO"
    print(f"[DONE/{status}] Novos: {novos} | Atualizados: {atualizados} | Sem mudança: {sem_mudanca} "
          f"| Checkpoint: {len(checkpoint)} animes marcados.")


def mode_all(api_key: str, user_id: str, limit: int = 0):
    print("[INFO] Iniciando modo ALL (incremental)...")
    lib = load_library()
    _ensure_full_ids(lib, user_id, api_key)  # sem isso, IDs truncados fariam tudo ser re-raspado e duplicado
    # a biblioteca é a fonte da verdade (CI não persiste arquivos locais); IDs legados "id-" normalizados
    known = {_clean_id(a["id"]) for a in lib["animes"]}
    all_series = get_all_series(user_id, api_key)

    new_count = 0
    try:
        for s in all_series:
            if s["id"] in known: continue
            if limit and new_count >= limit: break
            result = scrape_anime(s["id"], s, api_key, user_id)
            if result:
                lib["animes"].append(result)
                known.add(s["id"])
                new_count += 1
                print(f"[OK] Adicionado: {result['name']} ({len(result['episodes'])} eps)")
                if new_count % 5 == 0: save_library(lib)  # checkpoint
    finally:
        save_library(lib)
    print(f"[DONE] Novo(s) anime(s): {new_count}. Biblioteca salva.")


def mode_update_library(api_key: str, user_id: str = ""):
    print("[INFO] Iniciando UPDATE-LIBRARY (renova URLs)...")
    lib = load_library()
    sem_id = 0
    try:
        _ensure_full_ids(lib, user_id, api_key)  # repara IDs truncados (causa do HTTP 400 "itemId not valid")
    except requests.RequestException as e:
        print(f"[WARN] Falha ao reparar IDs truncados: {e}")
    total = len(lib["animes"])
    eps_ok = eps_fail = 0
    falhas_auth_seguidas = 0

    try:
        for n, anime in enumerate(lib["animes"], 1):
            for ep in anime.get("episodes", []):
                if not _is_full_id(_clean_id(ep.get("id") or "")):
                    # ID truncado sem correspondência: nem adianta pedir ao servidor (daria HTTP 400)
                    eps_fail += 1
                    sem_id += 1
                    continue
                resolved = _resolve_episode(ep["id"], api_key, user_id)
                if not resolved:
                    eps_fail += 1
                    # só falhas 401/403 contam para o abort; episódio removido/sem áudio não
                    if _STATE["auth_fail"]:
                        falhas_auth_seguidas += 1
                        if falhas_auth_seguidas >= MAX_FALHAS_SEGUIDAS:
                            raise RuntimeError(
                                f"{falhas_auth_seguidas} falhas de autenticação (401/403) seguidas. "
                                "Abortando para não gastar a execução inteira. Verifique ANISTREAM_API_KEY, "
                                "permissões do usuário no Jellyfin e bloqueio por IP/WAF."
                            )
                    continue
                falhas_auth_seguidas = 0
                ep["url"] = resolved["url"]
                ep["sub"] = resolved["sub"]  # se a legenda sumiu do servidor, limpa a antiga
                eps_ok += 1
            if n % 10 == 0: print(f"[PROGRESS] {n}/{total}")
    finally:
        save_library(lib)
    if sem_id:
        print(f"[WARN] {sem_id} episódios com ID truncado não puderam ser reparados "
              "(rode --mode scrap --anime <nome> para re-raspar esses animes).")
    print(f"[DONE] {eps_ok} episódios renovados em {total} animes ({eps_fail} falhas).")


def mode_patch_logos(api_key: str, user_id: str):
    """Backfill sem re-scrap: preenche logo_url (e poster) vazios usando uma única chamada a
    get_all_series. Match por id; se o id não existir no Jellyfin, tenta por nome (se único)."""
    print("[INFO] Iniciando PATCH-LOGOS (backfill sem re-scrap)...")
    lib = load_library()
    campos = (("logo_url", logo_url), ("poster", poster_url))

    if not any(not a.get(campo) for a in lib["animes"] for campo, _ in campos):
        print("[DONE] Nada a preencher.")
        return

    all_series = get_all_series(user_id, api_key)
    by_id = {s["id"]: s for s in all_series}
    by_name: Dict[str, List[Dict]] = {}
    for s in all_series:
        by_name.setdefault(_norm(s["name"]), []).append(s)

    filled = {campo: 0 for campo, _ in campos}
    not_found = 0
    for anime in lib["animes"]:
        if all(anime.get(campo) for campo, _ in campos):
            continue
        serie = by_id.get(_clean_id(anime["id"]))
        if serie is None:
            candidatos = by_name.get(_norm(anime.get("name")), [])
            serie = candidatos[0] if len(candidatos) == 1 else None
        if serie is None:
            print(f"[WARN] '{anime.get('name')}' não encontrado no Jellyfin (id/nome). Pulando.")
            not_found += 1
            continue
        for campo, builder in campos:
            if not anime.get(campo):
                value = builder(serie)
                if value:
                    anime[campo] = value
                    filled[campo] += 1

    save_library(lib)
    print(f"[DONE] logo_url: {filled['logo_url']} | poster: {filled['poster']} | não encontrados: {not_found}.")


def mode_scrap(api_key: str, user_id: str, name: str):
    print(f"[INFO] Buscando '{name}'...")
    all_series = get_all_series(user_id, api_key)
    q = _norm(name)
    exact = [s for s in all_series if _norm(s["name"]) == q]
    matches = exact or [s for s in all_series if q in _norm(s["name"])]
    if not matches:
        print("[ERROR] Anime não encontrado.")
        return
    if not exact and len(matches) > 1:
        nomes = ", ".join(m["name"] for m in matches[:5])
        print(f"[WARN] {len(matches)} resultados parciais ({nomes}{'...' if len(matches) > 5 else ''}). Usando o primeiro.")

    target = matches[0]
    result = scrape_anime(target["id"], target, api_key, user_id)
    if not result:
        return

    lib = load_library()
    _ensure_full_ids(lib, user_id, api_key)
    for i, a in enumerate(lib["animes"]):
        if _clean_id(a["id"]) == result["id"]:
            lib["animes"][i] = result  # re-scrap substitui (inclusive entrada com ID legado "id-"), não duplica
            break
    else:
        lib["animes"].append(result)
    save_library(lib)
    print(f"[OK] Scrapado: {result['name']}")


def mode_farm(api_key: str, user_id: str, count: int = 5):
    lib = load_library()
    _ensure_full_ids(lib, user_id, api_key)
    known = {_clean_id(a["id"]) for a in lib["animes"]}
    available = [s for s in get_all_series(user_id, api_key)
                 if s["id"] not in known and not is_nsfw(s["metadata"])]
    if not available:
        print("[FARM] Nenhum anime novo disponível.")
        return

    pick = random.sample(available, min(count, len(available)))
    try:
        for s in pick:
            result = scrape_anime(s["id"], s, api_key, user_id)
            if result:
                lib["animes"].append(result)
                print(f"[FARM] Adicionado: {result['name']}")
    finally:
        save_library(lib)


def mode_fix_names(api_key: str):
    # Estrutura para integração com AniList GraphQL
    # Requer TOKEN_ANILIST nas variáveis de ambiente
    print("[INFO] Fix-names aguarda implementação de mutação AniList. Placeholder pronto.")
    pass


def mode_import_jsons(api_key: str):
    lib = load_library()
    existing_ids = {_clean_id(a["id"]) for a in lib["animes"]}
    files = sorted(glob.glob("./imports/*.json"))
    merged = 0
    for fpath in files:
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                chunk = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"[WARN] Ignorando {fpath}: {e}")
            continue
        if not isinstance(chunk, dict):
            print(f"[WARN] Ignorando {fpath}: raiz do JSON não é um objeto com a chave 'animes'.")
            continue
        for anime in chunk.get("animes") or []:
            if not isinstance(anime, dict) or not anime.get("id"):
                print(f"[WARN] Anime sem 'id' (ou formato inválido) em {fpath}. Pulando.")
                continue
            aid = _clean_id(anime["id"])
            if aid in existing_ids:
                continue
            if is_nsfw({"Name": anime.get("name")}):
                print(f"[SKIP] Conteúdo NSFW filtrado na importação: {anime.get('name')}")
                continue
            lib["animes"].append(anime)
            existing_ids.add(aid)
            merged += 1
    save_library(lib)
    print(f"[IMPORT] {merged} animes fundidos.")


# ──────────────────────────────────────────────────────────────────────────────
# CLI & MAIN
# ──────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="AniStream Scraper & Library Manager")
    parser.add_argument("--mode", choices=["sync", "all", "update-library", "patch-logos", "scrap", "farm", "fix-names", "import-jsons"], required=True)
    parser.add_argument("--anime", help="Nome exato ou parcial para modo scrap")
    parser.add_argument("--farm-count", type=int, default=5, help="Quantidade para modo farm")
    parser.add_argument("--limit", type=int, default=0, help="Limite de itens para modo all")
    parser.add_argument("--max-runtime", type=int, default=0,
                        help="Tempo máximo de execução em minutos para modo sync (0=sem limite). "
                             "Salva checkpoint e para limpo quando o tempo for atingido.")
    parser.add_argument("--api-key", help="API key do Jellyfin (padrão: env ANISTREAM_API_KEY; prefira a variável de ambiente)")
    parser.add_argument("--user-id", help="User ID do Jellyfin (padrão: env ANISTREAM_USER_ID)")
    args = parser.parse_args()

    if args.mode == "scrap" and not args.anime:
        parser.error("--mode scrap requer --anime")

    # Argumento de linha de comando tem prioridade; senão usa a variável de ambiente.
    # strip(): secrets colados no GitHub costumam vir com espaço/quebra de linha no fim
    api_key = (args.api_key or os.environ.get("ANISTREAM_API_KEY", "")).strip().strip('"').strip("'")
    user_id = (args.user_id or os.environ.get("ANISTREAM_USER_ID", "")).strip().strip('"').strip("'")

    # Cada modo exige apenas as credenciais que realmente usa.
    needs_key = args.mode not in ("import-jsons", "fix-names")
    needs_user = args.mode in ("sync", "all", "scrap", "farm", "patch-logos")
    if needs_key and not api_key:
        print("[FATAL] API key ausente: use --api-key ou defina ANISTREAM_API_KEY.")
        sys.exit(1)
    if needs_user and not user_id:
        print("[FATAL] User ID ausente: use --user-id ou defina ANISTREAM_USER_ID.")
        sys.exit(1)
    if api_key:
        os.environ["ANISTREAM_API_KEY"] = api_key  # mantém compatibilidade com os helpers de API

    if args.mode in ("sync", "all", "scrap", "farm") and not any(
            FILTERS[k] for k in ("genres", "tags", "keywords", "title_blocks")):
        print("[WARN] Filtros NSFW vazios (variáveis NSFW_*_B64 ausentes ou inválidas): "
              "NENHUM conteúdo será filtrado nesta execução.")

    routes = {
        "sync": lambda: mode_sync(api_key, user_id, args.max_runtime),
        "all": lambda: mode_all(api_key, user_id, args.limit),
        "update-library": lambda: mode_update_library(api_key, user_id),
        "patch-logos": lambda: mode_patch_logos(api_key, user_id),
        "scrap": lambda: mode_scrap(api_key, user_id, args.anime),
        "farm": lambda: mode_farm(api_key, user_id, args.farm_count),
        "fix-names": lambda: mode_fix_names(api_key),
        "import-jsons": lambda: mode_import_jsons(api_key),
    }

    try:
        routes[args.mode]()
    except KeyboardInterrupt:
        print("\n[INTERRUPT] Operação abortada pelo usuário.")
        sys.exit(130)
    except Exception as e:
        print(f"\n[FATAL] Erro inesperado: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
