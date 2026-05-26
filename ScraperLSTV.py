#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import time
import logging
from dataclasses import dataclass, asdict
from typing import List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)


@dataclass
class StreamInfo:
    match_id: str
    commentator_id: Optional[int]
    home_name: str
    away_name: str
    commentator: str
    league: str
    kickoff: int
    home_logo: Optional[str]
    away_logo: Optional[str]
    m3u8: Optional[str]
    flv: Optional[str]
    slug: Optional[str]


class CDNOKScraper:
    API_MATCHES = "https://api-ls.cdnokvip.com/api/get-livestream-group"
    API_DETAIL = "https://api-ls.cdnokvip.com/api/match-detail-slug"

    def __init__(self, workers: int = 30):
        self.workers = workers
        self.session = self._init_session()
        self.slug_cache = {}

    def _init_session(self) -> requests.Session:
        session = requests.Session()
        retry = Retry(total=2, backoff_factor=0.2,
                      status_forcelist=[429, 500, 502, 503, 504])
        adapter = HTTPAdapter(pool_connections=100, pool_maxsize=100, max_retries=retry)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        session.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
            "Connection": "keep-alive",
        })
        return session

    def get_matches(self) -> List[dict]:
        params = {
            "isHot": "false", "isLive": "false",
            "isToday": "false", "isTomorrow": "false",
            "offset": 0, "_t": int(time.time() * 1000)
        }
        r = self.session.get(self.API_MATCHES, params=params, timeout=15)
        r.raise_for_status()
        return r.json().get("value", {}).get("datas", [])

    def get_match_detail(self, slug: str) -> Optional[dict]:
        if not slug:
            return None
        if slug in self.slug_cache:
            return self.slug_cache[slug]
        try:
            r = self.session.get(self.API_DETAIL, params={"slug": slug}, timeout=15)
            r.raise_for_status()
            data = r.json().get("value", {}).get("datas")
            self.slug_cache[slug] = data
            return data
        except Exception:
            return None

    def parse_stream(self, detail: dict) -> StreamInfo:
        return StreamInfo(
            match_id=detail.get("matchId", ""),
            commentator_id=detail.get("commentatorId"),
            home_name=detail.get("homeName") or "Unknown",
            away_name=detail.get("awayName") or "Unknown",
            commentator=detail.get("commentator") or "N/A",
            league=detail.get("leagueShortName") or detail.get("leagueName") or "Football",
            kickoff=detail.get("matchTime", 0),
            home_logo=detail.get("homeLogo"),
            away_logo=detail.get("awayLogo"),
            m3u8=detail.get("linkLive"),
            flv=detail.get("linkLiveFlv"),
            slug=detail.get("slugUrl"),
        )

    def fetch_slug(self, slug: str) -> List[dict]:
        detail = self.get_match_detail(slug)
        if not detail:
            return []
        results = [detail]
        for c in detail.get("listCommentators") or []:
            sub_detail = self.get_match_detail(c.get("slugUrl"))
            if sub_detail:
                results.append(sub_detail)
        return results

    def get_all_streams(self) -> List[StreamInfo]:
        matches = self.get_matches()
        logging.info("Found %d matches", len(matches))

        slugs = {m.get("slugUrl") for m in matches if m.get("slugUrl")}
        streams, seen = [], set()

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = {executor.submit(self.fetch_slug, slug): slug for slug in slugs}
            for future in as_completed(futures):
                for detail in future.result() or []:
                    try:
                        stream = self.parse_stream(detail)
                        key = (stream.match_id, stream.commentator_id)
                        if key not in seen:
                            seen.add(key)
                            streams.append(stream)
                            logging.info("%s vs %s | %s",
                                         stream.home_name, stream.away_name, stream.commentator)
                    except Exception as e:
                        logging.error("Parse error: %s", e)
        return streams

    def export_json(self, streams: List[StreamInfo], filename="streams.json"):
        with open(filename, "w", encoding="utf-8") as f:
            json.dump([asdict(s) for s in streams], f, indent=2, ensure_ascii=False)
        logging.info("JSON saved: %s", filename)

    def export_m3u(self, streams: List[StreamInfo], filename="streams.m3u"):
        with open(filename, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n\n")
            for s in streams:
                title = f"{s.home_name} vs {s.away_name}"
                logo = s.home_logo or ""
                group = s.league or "Football"
                for link, suffix in [(s.m3u8, ""), (s.flv, " FLV")]:
                    if link:
                        f.write(
                            f'#EXTINF:-1 tvg-id="{s.match_id}{ "_flv" if suffix else ""}" '
                            f'tvg-name="{title}" tvg-logo="{logo}" '
                            f'group-title="{group}",{title}{suffix} | {s.commentator}\n{link}\n\n'
                        )
        logging.info("M3U saved: %s", filename)

    def export_monplayer_json(self, streams: List[StreamInfo], filename="monplayer.json"):
        data = []
        for s in streams:
            for link, suffix in [(s.m3u8, ""), (s.flv, " FLV")]:
                if link:  # chỉ giữ lại trận có link
                    kickoff_str = ""
                    if s.kickoff:
                        try:
                            kickoff_str = datetime.fromtimestamp(s.kickoff).strftime("%Y-%m-%d %H:%M")
                        except Exception:
                            kickoff_str = str(s.kickoff)
                    data.append({
                        "name": f"{s.home_name} vs {s.away_name}{suffix}",
                        "url": link,
                        "logo": s.home_logo or "",
                        "group": s.league or "Football",
                        "commentator": s.commentator or "N/A",
                        "kickoff": kickoff_str
                    })
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logging.info("MonPlayer JSON saved: %s", filename)


def main():
    start = time.time()
    scraper = CDNOKScraper(workers=40)
    streams = scraper.get_all_streams()
    logging.info("Total streams: %d", len(streams))
    scraper.export_json(streams, filename=f"tv.json")
    scraper.export_m3u(streams, filename=f"tv.m3u")
    scraper.export_monplayer_json(streams, filename=f"mon.json")

    logging.info("Done in %.2fs", time.time() - start)


if __name__ == "__main__":
    main()
