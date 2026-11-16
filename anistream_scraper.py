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
REQUEST_TIMEOUT = 15  # segundos
PAGE_SIZE = 500       # itens por página nas listagens do Jellyfin

# DeviceId fixo — o servidor Jellyfin usa para rastrear sessões; não alterar.
DEVICE_ID = "a9d0ebde-e77e-48d6-a6ee-cbfa9c991c1f"

# Parâmetros fixos da URL HLS (main.m3u8).
# AudioCodec=copy: browsers não decodificam AC3/EAC3 via MSE; o frontend converte
# copy→aac em fixUrl() e o Jellyfin transcodifica quando o perfil do device pede AAC.
STREAM_BASE = {"AudioCodec": "copy"}

# Idiomas aceitos como português. O Jellyfin costuma usar ISO 639-2 ("por"), não "pt".
PT_LANGS = {"pt", "por", "pt-br", "ptbr", "pob"}

# 🔒 SEGURANÇA: Valores base64 devem permanecer codificados no source.
# Decodificados apenas em memória durante a execução.
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


def _headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": (
            f'MediaBrowser Token="{api_key}", Client="Jellyfin Web", '
            f'Device="Chrome", DeviceId="{DEVICE_ID}", Version="10.9.11"'
        ),
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Connection": "keep-alive",
    }


def _api_get(endpoint: str, params: Optional[Dict] = None, **kwargs) -> requests.Response:
    url = f"{BASE_URL}{endpoint}"
    if params:
        url += f"?{urlencode(params)}"
    api_key = kwargs.pop("apikey", None) or os.environ["ANISTREAM_API_KEY"]
    return requests.get(url, headers=_headers(api_key), timeout=REQUEST_TIMEOUT)


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


# ──────────────────────────────────────────────────────────────────────────────
# FUNÇÕES DE MÍNIMA UTILIDADE
# ──────────────────────────────────────────────────────────────────────────────
def load_library(path: str = LIBRARY_PATH) -> Dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"app": "AniLib", "animes": []}


def save_library(lib: Dict, path: str = LIBRARY_PATH):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(lib, f, indent=2, ensure_ascii=False)


def _exists_in_lib(anime_id: str, lib: Dict) -> bool:
    return any(a["id"] == anime_id for a in lib["animes"])


def _find_anime_by_id(anime_id: str, lib: Dict) -> Optional[Dict]:
    return next((a for a in lib["animes"] if a["id"] == anime_id), None)


def _norm(text: Optional[str]) -> str:
    """Normaliza nome para comparação (case-insensitive, espaços colapsados)."""
    return " ".join((text or "").casefold().split())


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
            "Fields": "Genres,Tags,OfficialRating,ProductionYear,Overview,ImageTags",
            "SortBy": "SortName",  # ordem estável, necessária para paginar sem duplicar
            "StartIndex": start,
            "Limit": PAGE_SIZE,
        }
        resp = _api_get(f"/Users/{user_id}/Items", params=params, apikey=api_key)
        _check(resp, "listagem de séries (/Users/<id>/Items)")
        data = resp.json()
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
        data = resp.json()
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


def _detect_pt_streams(item_id: str, api_key: str) -> Optional[Tuple[int, int]]:
    """Retorna (audio_idx, sub_idx) — -1 quando a faixa não existe — ou None em caso de falha.
    Printa erro explícito em caso de falha HTTP/autenticação/rede."""
    try:
        resp = _api_get(f"/Items/{item_id}?Fields=MediaStreams", apikey=api_key)
    except requests.RequestException as e:
        print(f"[ERROR] Falha de rede ao buscar streams do item {item_id}: {e}")
        return None

    if resp.status_code in (401, 403):
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
    item_id = serie.get("id") or meta.get("Id")
    return f"{BASE_URL}/Items/{item_id}/Images/{kind}?tag={tag}&{size_qs}"


def logo_url(serie: Dict) -> str:
    """Gera URL de logo sem depender de ImageTags.Logo (nem sempre vem na listagem).
    O servidor retorna 404 silencioso se a logo não existir — o frontend trata com onerror."""
    meta = serie.get("metadata", serie)
    item_id = serie.get("id") or meta.get("Id") or ""
    if not item_id:
        return ""
    # usa tag se disponível (cache-busting), senão manda sem — funciona do mesmo jeito
    tag = (meta.get("ImageTags") or {}).get("Logo")
    qs = f"tag={tag}&" if tag else ""
    return f"{BASE_URL}/Items/{item_id}/Images/Logo?{qs}fillWidth=1344&fillHeight=256&quality=75"


def poster_url(serie: Dict) -> str:
    return _image_url(serie, "Primary", "fillWidth=400&quality=85")


def _resolve_episode(ep_id: str, api_key: str) -> Optional[Dict]:
    """Detecta streams PT, consulta PlaybackInfo e monta {"url", "sub"}. None se falhar."""
    streams = _detect_pt_streams(ep_id, api_key)
    if streams is None:
        return None
    audio_idx, sub_idx = streams
    if audio_idx < 0:
        print(f"[WARN] Sem faixa de áudio utilizável em {ep_id}. Pulando.")
        return None

    try:
        playback = _playback_info_post(ep_id, api_key, audio_idx)
    except requests.RequestException as e:
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
    name_lower = serie_meta.get("Name", "").lower()
    genres = [g.lower() for g in serie_meta.get("Genres", [])]
    tags = [t.lower() for t in serie_meta.get("Tags", [])]

    # Whitelist verifica legendas
    whitelist_subs = FILTERS.get("whitelist_subs", [])
    # Se tiver legenda na whitelist, ignora filtro
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
        resolved = _resolve_episode(ep["Id"], api_key)
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


def mode_all(api_key: str, user_id: str, limit: int = 0):
    print("[INFO] Iniciando modo ALL (incremental)...")
    lib = load_library()
    known = {a["id"] for a in lib["animes"]}  # a biblioteca é a fonte da verdade (CI não persiste arquivos locais)
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


def mode_update_library(api_key: str):
    print("[INFO] Iniciando UPDATE-LIBRARY (renova URLs)...")
    lib = load_library()
    total = len(lib["animes"])
    eps_ok = eps_fail = 0

    try:
        for n, anime in enumerate(lib["animes"], 1):
            for ep in anime.get("episodes", []):
                resolved = _resolve_episode(ep["id"], api_key)
                if not resolved:
                    eps_fail += 1
                    continue
                ep["url"] = resolved["url"]
                if resolved["sub"]:
                    ep["sub"] = resolved["sub"]
                eps_ok += 1
            if n % 10 == 0: print(f"[PROGRESS] {n}/{total}")
    finally:
        save_library(lib)
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
        serie = by_id.get(anime["id"])
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
    for i, a in enumerate(lib["animes"]):
        if a["id"] == result["id"]:
            lib["animes"][i] = result  # re-scrap substitui, não duplica
            break
    else:
        lib["animes"].append(result)
    save_library(lib)
    print(f"[OK] Scrapado: {result['name']}")


def mode_farm(api_key: str, user_id: str, count: int = 5):
    lib = load_library()
    known = {a["id"] for a in lib["animes"]}
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
    existing_ids = {a["id"] for a in lib["animes"]}
    files = sorted(glob.glob("./imports/*.json"))
    merged = 0
    for fpath in files:
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                chunk = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"[WARN] Ignorando {fpath}: {e}")
            continue
        for anime in chunk.get("animes", []):
            if anime["id"] not in existing_ids:
                lib["animes"].append(anime)
                existing_ids.add(anime["id"])
                merged += 1
    save_library(lib)
    print(f"[IMPORT] {merged} animes fundidos.")


# ──────────────────────────────────────────────────────────────────────────────
# CLI & MAIN
# ──────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="AniStream Scraper & Library Manager")
    parser.add_argument("--mode", choices=["all", "update-library", "patch-logos", "scrap", "farm", "fix-names", "import-jsons"], required=True)
    parser.add_argument("--anime", help="Nome exato ou parcial para modo scrap")
    parser.add_argument("--farm-count", type=int, default=5, help="Quantidade para modo farm")
    parser.add_argument("--limit", type=int, default=0, help="Limite de itens para modo all")
    parser.add_argument("--api-key", help="API key do Jellyfin (padrão: env ANISTREAM_API_KEY)")
    parser.add_argument("--user-id", help="User ID do Jellyfin (padrão: env ANISTREAM_USER_ID)")
    args = parser.parse_args()

    if args.mode == "scrap" and not args.anime:
        parser.error("--mode scrap requer --anime")

    # Argumento de linha de comando tem prioridade; senão usa a variável de ambiente.
    api_key = args.api_key or os.environ.get("ANISTREAM_API_KEY", "")
    user_id = args.user_id or os.environ.get("ANISTREAM_USER_ID", "")
    if not api_key:
        print("[FATAL] API key ausente: use --api-key ou defina ANISTREAM_API_KEY.")
        sys.exit(1)
    if not user_id:
        print("[FATAL] User ID ausente: use --user-id ou defina ANISTREAM_USER_ID.")
        sys.exit(1)
    os.environ["ANISTREAM_API_KEY"] = api_key  # mantém compatibilidade com os helpers de API

    if args.mode in ("all", "scrap", "farm") and not any(
            FILTERS[k] for k in ("genres", "tags", "keywords", "title_blocks")):
        print("[WARN] Filtros NSFW vazios (variáveis NSFW_*_B64 ausentes ou inválidas): "
              "NENHUM conteúdo será filtrado nesta execução.")

    routes = {
        "all": lambda: mode_all(api_key, user_id, args.limit),
        "update-library": lambda: mode_update_library(api_key),
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
        sys.exit(0)
    except Exception as e:
        print(f"\n[FATAL] Erro inesperado: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
