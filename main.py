"""Fetch and query Sanguosha battle records from the Qunying Zhanbao website."""

from __future__ import annotations

import asyncio
import html
import json
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from difflib import get_close_matches
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

DEFAULT_BASE_URL = "http://124.223.83.131:5000/"
SHANGHAI_TZ = timezone(timedelta(hours=8))
ROLES = ("主公", "忠臣", "反贼", "内奸")
ROLE_CLASS_MAP = {
    "lord": "主公",
    "loyal": "忠臣",
    "rebel": "反贼",
    "spy": "内奸",
}
PAGE_SIZE = 100

_CARD_RE = re.compile(r'<details class="game-card"[^>]*>(.*?)</details>', re.S)
_GAME_DATE_RE = re.compile(
    r'<div class="game-date"><strong>(.*?)</strong><span>(.*?)</span></div>',
    re.S,
)
_GAME_HEAD_RE = re.compile(
    r'<div class="game-heading"><b>(.*?)</b><span>(.*?)</span></div>',
    re.S,
)
_TBODY_RE = re.compile(r"<tbody>(.*?)</tbody>", re.S)
_ROW_RE = re.compile(r'<tr class="[^"]*">(.*?)</tr>', re.S)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_PLAYER_GENERAL_RE = re.compile(r"<b>(.*?)</b><small>(.*?)</small>", re.S)
_ROLE_RE = re.compile(r'<span class="role role-(\w+)">(.*?)</span>', re.S)
_RESULT_RE = re.compile(
    r'<td><span class="(?:positive|muted)">(.*?)</span><small>(.*?)</small></td>',
    re.S,
)
_GAME_ID_RE = re.compile(r'<div class="game-foot"><code>(.*?)</code>', re.S)
_TOTAL_RE = re.compile(r"共\s*(\d+)\s*局")
_PLAYER_COUNT_RE = re.compile(r"/\s*(\d+)\s*人局")
_HEADING_META_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})\s*·\s*房间\s*(\d+)\s*·\s*(.+)",
    re.S,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _text(fragment: str) -> str:
    """Convert an HTML fragment to trimmed plain text."""
    return html.unescape(_TAG_RE.sub("", fragment)).strip()


def _to_int(value: str) -> int | None:
    """Convert a table cell to int, returning None for placeholders like a dash."""
    value = value.replace("+", "").replace("−", "-").strip()
    return int(value) if re.fullmatch(r"-?\d+", value) else None


def _parse_games(page_html: str) -> list[dict]:
    """Parse all battle cards from one HTML page.

    Args:
        page_html: Raw HTML of a battle list page.

    Returns:
        A list of game dictionaries, each containing player records.
    """
    games: list[dict] = []
    for card in _CARD_RE.findall(page_html):
        head_match = _GAME_HEAD_RE.search(card)
        if not head_match:
            continue
        date_match = _GAME_DATE_RE.search(card)
        title = _text(head_match.group(1))
        player_count_match = _PLAYER_COUNT_RE.search(title)
        meta_match = _HEADING_META_RE.search(_text(head_match.group(2)))
        game_id_match = _GAME_ID_RE.search(card)
        game = {
            "id": game_id_match.group(1) if game_id_match else "",
            "mode": title.split("/")[0].strip(),
            "player_count": (
                int(player_count_match.group(1)) if player_count_match else 0
            ),
            "date": meta_match.group(1) if meta_match else "",
            "room": meta_match.group(2) if meta_match else "",
            "winner": meta_match.group(3).strip() if meta_match else "",
            "time": _text(date_match.group(2)) if date_match else "",
            "players": [],
        }
        try:
            game["datetime"] = datetime.strptime(
                f"{game['date']} {game['time']}",
                "%Y-%m-%d %H:%M",
            )
        except ValueError:
            game["datetime"] = None

        body_match = _TBODY_RE.search(card)
        if body_match:
            for row in _ROW_RE.findall(body_match.group(1)):
                cells = _TD_RE.findall(row)
                player_general_match = _PLAYER_GENERAL_RE.search(row)
                if len(cells) < 10 or not player_general_match:
                    continue
                role_match = _ROLE_RE.search(row)
                result_match = _RESULT_RE.search(row)
                values = [_text(cell) for cell in cells]
                game["players"].append(
                    {
                        "seat": values[0],
                        "player": _text(player_general_match.group(1)),
                        "general": _text(player_general_match.group(2)),
                        "role": (
                            ROLE_CLASS_MAP.get(
                                role_match.group(1),
                                _text(role_match.group(2)),
                            )
                            if role_match
                            else values[2]
                        ),
                        "result": (
                            _text(result_match.group(1))
                            if result_match
                            else values[3]
                        ),
                        "survival": (
                            _text(result_match.group(2)) if result_match else ""
                        ),
                        "score": _to_int(values[4]),
                        "turns": _to_int(values[5]),
                        "damage": _to_int(values[6]),
                        "taken": _to_int(values[7]),
                        "heal": _to_int(values[8]),
                        "kills": _to_int(values[9]),
                        "date": game["date"],
                        "time": game["time"],
                        "game_id": game["id"],
                        "player_count": game["player_count"],
                        "mode": game["mode"],
                        "winner": game["winner"],
                    }
                )
        games.append(game)
    return games


def _dedupe_games(games: list[dict]) -> list[dict]:
    """Remove duplicate games and sort them from newest to oldest."""
    seen: set[str] = set()
    unique: list[dict] = []
    for game in games:
        key = game["id"] or f"{game['date']} {game['time']} {game['room']}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(game)
    unique.sort(
        key=lambda game: (game["date"], game["time"]),
        reverse=True,
    )
    return unique


def _summary(records: list[dict]) -> dict:
    """Aggregate wins, losses, draws, score and identity counts."""
    total = len(records)
    wins = sum(1 for record in records if record["result"] == "胜利")
    draws = sum(1 for record in records if record["result"] == "平局")
    roles = Counter(record["role"] for record in records)
    return {
        "games": total,
        "wins": wins,
        "losses": total - wins - draws,
        "draws": draws,
        "win_rate": round(wins / total, 4) if total else 0.0,
        "score": sum(
            record["score"] for record in records if record["score"] is not None
        ),
        "roles": {role: roles.get(role, 0) for role in ROLES},
    }


def _role_rates(roles: dict[str, int], total: int) -> dict[str, float]:
    """Convert identity counts to probabilities."""
    return {
        role: (round(roles.get(role, 0) / total, 4) if total else 0.0)
        for role in ROLES
    }


def _resolve_name(query: str, names: list[str]) -> tuple[str | None, list[str]]:
    """Resolve a player or general name with exact, substring and fuzzy matching."""
    query = query.strip()
    if not query:
        return None, []
    folded = query.casefold()
    for name in names:
        if name.casefold() == folded:
            return name, []
    contains = [name for name in names if folded in name.casefold()]
    if len(contains) == 1:
        return contains[0], []
    if contains:
        return None, contains[:10]
    return None, get_close_matches(query, names, n=5, cutoff=0.5)


def _exact_name(query: str, names: list[str]) -> str | None:
    """Return an exact case-insensitive match, if any."""
    folded = query.strip().casefold()
    if not folded:
        return None
    return next(
        (name for name in names if name.casefold() == folded),
        None,
    )


def _latest_text(games: list[dict]) -> str:
    """Return the newest game time as display text."""
    latest = max(
        (game for game in games if game.get("datetime")),
        key=lambda game: game["datetime"],
        default=None,
    )
    return f"{latest['date']} {latest['time']}" if latest else "未知"


def _record_line(summary: dict) -> str:
    """Format the win/loss/draw part of a stats line."""
    return (
        f"{summary['games']} 局 · {summary['wins']}胜 "
        f"{summary['losses']}负 {summary['draws']}平 · "
        f"胜率 {summary['win_rate'] * 100:.1f}%"
    )


def _role_line(roles: dict[str, int], total: int) -> str:
    """Format identity counts with percentages."""
    if not total:
        return "无"
    return " / ".join(
        f"{role} {roles.get(role, 0)}（{roles.get(role, 0) / total * 100:.1f}%）"
        for role in ROLES
    )


def _role_compact(roles: dict[str, int]) -> str:
    """Format non-zero identity counts compactly."""
    parts = [f"{role}{roles[role]}" for role in ROLES if roles.get(role)]
    return " / ".join(parts) if parts else "无"


def _score_text(score: int) -> str:
    """Format a score with an explicit sign."""
    return f"{score:+d}" if score else "0"


def _group_records(records: list[dict], key: str) -> dict[str, list[dict]]:
    """Group player records by a field such as player or general."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        groups[record[key]].append(record)
    return groups


class GetZhanJiPlugin(Star):
    """AstrBot plugin for querying Sanguosha battle records."""

    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | None = None,
    ) -> None:
        """Initialize the plugin and its cache.

        Args:
            context: AstrBot plugin context.
            config: Plugin configuration loaded from _conf_schema.json.
        """
        super().__init__(context)
        self.config = config or {}
        self.base_url = (
            str(self.config.get("base_url") or DEFAULT_BASE_URL).strip().rstrip("/")
            + "/"
        )
        self.cache_ttl = max(0, int(self.config.get("cache_ttl", 600)))
        self.timeout = max(1, int(self.config.get("timeout", 20)))
        self.default_days = max(0, int(self.config.get("default_days", 0)))
        self.max_pages = max(1, int(self.config.get("max_pages", 50)))
        self.profiles_path = self._resolve_profiles_path()
        logger.info("Player profiles path: %s", self.profiles_path)
        self._profiles: dict[str, dict] = {}
        self._profiles_mtime = -1.0
        self._games: list[dict] = []
        self._cached_at = 0.0
        self._cached_wall = ""
        self._lock = asyncio.Lock()

    def _cache_fresh(self) -> bool:
        """Return whether the in-memory cache is still valid."""
        return bool(self._games) and (
            time.monotonic() - self._cached_at < self.cache_ttl
        )

    def _fetch_page(self, page: int) -> str:
        """Fetch one page of battle records synchronously.

        Args:
            page: One-based page number.

        Returns:
            Decoded HTML of the page.

        Raises:
            OSError: If the HTTP request fails.
        """
        end = (datetime.now(SHANGHAI_TZ) + timedelta(days=1)).strftime(
            "%Y-%m-%dT%H:%M"
        )
        query = urlencode(
            {
                "start": "2000-01-01T00:00",
                "end": end,
                "page": page,
                "page_size": PAGE_SIZE,
            }
        )
        request = Request(
            f"{self.base_url}?{query}",
            headers={
                "User-Agent": "Mozilla/5.0 (AstrBot; astrbot_plugin_getZhanJi)",
            },
        )
        with urlopen(request, timeout=self.timeout) as response:
            return response.read().decode("utf-8", errors="replace")

    def _fetch_all_sync(self) -> list[dict]:
        """Fetch and parse every page of battle records.

        Returns:
            Parsed games sorted from newest to oldest.

        Raises:
            RuntimeError: If no game can be parsed from the website.
        """
        first_page = self._fetch_page(1)
        games = _parse_games(first_page)
        total_match = _TOTAL_RE.search(first_page)
        total = int(total_match.group(1)) if total_match else None
        if total is not None:
            pages = min(self.max_pages, max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE))
        else:
            pages = self.max_pages
        for page in range(2, pages + 1):
            page_games = _parse_games(self._fetch_page(page))
            games.extend(page_games)
            if total is None and len(page_games) < PAGE_SIZE:
                break
        games = _dedupe_games(games)
        if not games:
            raise RuntimeError("战绩网页中没有解析到对局数据")
        return games

    async def _load_games(
        self,
        force: bool = False,
        allow_stale: bool = True,
    ) -> list[dict]:
        """Load games from cache or the website.

        Args:
            force: Ignore the cache and fetch again.
            allow_stale: Return stale cache when a refresh fails.

        Returns:
            Parsed games sorted from newest to oldest.

        Raises:
            Exception: If the website cannot be fetched and no stale data exists.
        """
        async with self._lock:
            if not force and self._cache_fresh():
                return self._games
            try:
                games = await asyncio.to_thread(self._fetch_all_sync)
            except Exception as exc:
                if allow_stale and self._games:
                    logger.warning(
                        "Failed to refresh Sanguosha records (%s); using cache.",
                        exc,
                    )
                    return self._games
                raise
            self._games = games
            self._cached_at = time.monotonic()
            self._cached_wall = datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M")
            logger.info(
                "Loaded %d Sanguosha games from %s.",
                len(games),
                self.base_url,
            )
            return games

    def _filter_games(self, games: list[dict], days: int) -> list[dict]:
        """Keep games from the last ``days`` days."""
        if days <= 0:
            return games
        cutoff = datetime.now(SHANGHAI_TZ).replace(tzinfo=None) - timedelta(
            days=days
        )
        return [
            game
            for game in games
            if game.get("datetime") and game["datetime"] >= cutoff
        ]

    def _resolve_profiles_path(self) -> Path:
        """Resolve the persistent profile file and seed it from the template."""
        try:
            data_dir = StarTools.get_data_dir("astrbot_plugin_getZhanJi")
        except Exception as exc:
            logger.warning("Failed to resolve plugin data directory: %s", exc)
            data_dir = Path(__file__).parent
        profile_path = data_dir / "player_profiles.json"
        if not profile_path.exists():
            template_path = Path(__file__).with_name("player_profiles.json")
            try:
                if template_path.exists():
                    profile_path.write_text(
                        template_path.read_text(encoding="utf-8"),
                        encoding="utf-8",
                    )
            except OSError as exc:
                logger.warning("Failed to seed player profiles: %s", exc)
        return profile_path

    def _load_profiles(self) -> dict[str, dict]:
        """Load player profiles from JSON and reload when the file changes."""
        try:
            mtime = self.profiles_path.stat().st_mtime
        except OSError:
            return self._profiles
        if mtime == self._profiles_mtime:
            return self._profiles
        try:
            raw = json.loads(self.profiles_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to load player profiles: %s", exc)
            self._profiles_mtime = mtime
            return self._profiles
        profiles: dict[str, dict] = {}
        if isinstance(raw, dict):
            for name, item in raw.items():
                if name.startswith("_") or not isinstance(item, dict):
                    continue
                fields = {
                    key: str(item.get(key) or "").strip()
                    for key in ("personality", "style", "notes")
                }
                if any(fields.values()):
                    profiles[name.strip()] = fields
        self._profiles = profiles
        self._profiles_mtime = mtime
        return profiles

    def _get_profile(self, player: str) -> dict:
        """Return the manual profile for a player, if any."""
        profiles = self._load_profiles()
        profile = profiles.get(player)
        if profile:
            return profile
        folded = player.casefold()
        for name, item in profiles.items():
            if name.casefold() == folded:
                return item
        return {}

    @staticmethod
    def _profile_line(profile: dict) -> str:
        """Format a manual player profile as one display line."""
        parts = []
        if profile.get("personality"):
            parts.append(f"性格：{profile['personality']}")
        if profile.get("style"):
            parts.append(f"打法：{profile['style']}")
        if profile.get("notes"):
            parts.append(f"备注：{profile['notes']}")
        return " ｜ ".join(parts)

    @staticmethod
    def _style_metrics(records: list[dict]) -> dict:
        """Compute objective play-style metrics from player records."""
        def average(key: str) -> float:
            values = [
                record[key]
                for record in records
                if record.get(key) is not None
            ]
            return round(sum(values) / len(values), 2) if values else 0.0

        survivals = [
            record["survival"]
            for record in records
            if record.get("survival") in {"存活", "阵亡"}
        ]
        return {
            "avg_damage": average("damage"),
            "avg_kills": average("kills"),
            "avg_taken": average("taken"),
            "avg_heal": average("heal"),
            "avg_turns": average("turns"),
            "survival_rate": (
                round(survivals.count("存活") / len(survivals), 4)
                if survivals
                else 0.0
            ),
        }

    @staticmethod
    def _strip_command(message: str) -> str:
        """Remove the command word and an optional mention from the message."""
        text = message.strip()
        text = re.sub(r"^(?:@\S+\s*)+", "", text).strip()
        text = text.lstrip("/").strip()
        match = re.match(
            r"^(?:zhanji|战绩|sgs)\s*[：:，,]?\s*(.*)$",
            text,
            re.IGNORECASE | re.S,
        )
        return (match.group(1) if match else text).strip()

    def _help_text(self) -> str:
        """Build the help message."""
        return (
            "【三国杀战绩插件】\n"
            f"数据源：{self.base_url}\n"
            "指令：\n"
            "/zhanji 或 /战绩 —— 总览\n"
            "/zhanji 玩家 <昵称> —— 玩家统计\n"
            "/zhanji 武将 <武将> —— 武将统计\n"
            "/zhanji <昵称> <武将> —— 该玩家用该武将时的身份分布\n"
            "/zhanji 最近 [数量] —— 最近对局（1-10，默认 5）\n"
            "/zhanji 刷新 —— 强制重新抓取\n"
            "/zhanji help —— 本帮助\n\n"
            "玩家画像：编辑 AstrBot 数据目录下的 "
            "data/plugin_data/astrbot_plugin_getZhanJi/"
            "player_profiles.json（保存后自动生效）\n"
            "也可以直接 @我 用自然语言问，例如：\n"
            "「ranber 用孙权时最可能是什么身份？」\n"
            "「最近谁胜率最高？」\n"
            f"当前缓存时间：{self._cached_wall or '尚未抓取'}"
        )

    def _summary_text(self, games: list[dict], records: list[dict]) -> str:
        """Build the overview message."""
        summary = _summary(records)
        by_player = _group_records(records, "player")
        by_general = _group_records(records, "general")
        top_players = sorted(
            ((name, _summary(items)) for name, items in by_player.items()),
            key=lambda item: (
                -item[1]["score"],
                -item[1]["games"],
                item[0],
            ),
        )[:5]
        top_generals = sorted(
            ((name, _summary(items)) for name, items in by_general.items()),
            key=lambda item: (-item[1]["games"], item[0]),
        )[:5]
        lines = [
            f"【群英战报】{len(games)} 局 · "
            f"{games[-1]['date']} ~ {games[0]['date']}",
            f"最近对局：{_latest_text(games)} · "
            f"缓存于 {self._cached_wall or '未知'}",
            "",
            "积分榜前 5：",
        ]
        for index, (name, item) in enumerate(top_players, 1):
            lines.append(
                f"{index}. {name} {_score_text(item['score'])} 分"
                f"（{_record_line(item)}）"
            )
        lines.append("")
        lines.append("武将出场前 5：")
        for index, (name, item) in enumerate(top_generals, 1):
            lines.append(
                f"{index}. {name} {item['games']} 局 / "
                f"胜率 {item['win_rate'] * 100:.1f}%"
            )
        lines.append("")
        lines.append(
            f"全员身份分布：{_role_line(summary['roles'], summary['games'])}"
        )
        lines.append("发送「/zhanji help」查看全部用法。")
        return "\n".join(lines)

    def _player_text(self, player: str, records: list[dict]) -> str:
        """Build a player statistics message."""
        summary = _summary(records)
        by_general = _group_records(records, "general")
        top_generals = sorted(
            by_general.items(),
            key=lambda item: (-len(item[1]), item[0]),
        )[:5]
        profile = self._get_profile(player)
        metrics = self._style_metrics(records)
        lines = [
            f"【{player}】{_record_line(summary)}",
            f"总积分：{_score_text(summary['score'])}",
            f"身份分布：{_role_line(summary['roles'], summary['games'])}",
        ]
        if profile:
            lines.append(f"玩家画像：{self._profile_line(profile)}")
        lines.append(
            "数据风格："
            f"场均伤害 {metrics['avg_damage']} / "
            f"场均击杀 {metrics['avg_kills']} / "
            f"场均承伤 {metrics['avg_taken']} / "
            f"场均恢复 {metrics['avg_heal']} / "
            f"存活率 {metrics['survival_rate'] * 100:.1f}% / "
            f"平均回合 {metrics['avg_turns']}"
        )
        lines.append("常用武将（前 5）：")
        for general, items in top_generals:
            item = _summary(items)
            lines.append(
                f"· {general} {item['games']}局"
                f"（胜率 {item['win_rate'] * 100:.1f}%）："
                f"{_role_compact(item['roles'])}"
            )
        lines.append(f"查看某武将的身份分布：/zhanji {player} <武将>")
        return "\n".join(lines)

    def _general_text(self, general: str, records: list[dict]) -> str:
        """Build a general statistics message."""
        summary = _summary(records)
        by_player = _group_records(records, "player")
        top_players = sorted(
            by_player.items(),
            key=lambda item: (-len(item[1]), item[0]),
        )[:5]
        lines = [
            f"【{general}】{_record_line(summary)}",
            f"身份分布：{_role_line(summary['roles'], summary['games'])}",
            "常用玩家（前 5）：",
        ]
        for player, items in top_players:
            item = _summary(items)
            lines.append(
                f"· {player} {item['games']}局"
                f"（胜率 {item['win_rate'] * 100:.1f}%）："
                f"{_role_compact(item['roles'])}"
            )
        lines.append(f"查看某玩家用该武将：/zhanji <昵称> {general}")
        return "\n".join(lines)

    def _combo_text(
        self,
        player: str,
        general: str,
        combo_records: list[dict],
        player_records: list[dict],
        general_records: list[dict],
        all_records: list[dict],
    ) -> str:
        """Build the player-plus-general identity distribution message."""
        summary = _summary(combo_records)
        player_summary = _summary(player_records)
        general_summary = _summary(general_records)
        global_summary = _summary(all_records)
        total = summary["games"]
        lines = [
            f"【{player} × {general}】{_record_line(summary)}",
            "身份分布：",
        ]
        for role in ROLES:
            count = summary["roles"][role]
            lines.append(f"  {role} {count} 局（{count / total * 100:.1f}%）")
        max_count = max(summary["roles"].values())
        top_roles = [
            role
            for role in ROLES
            if summary["roles"][role] == max_count
        ]
        if len(top_roles) == 1:
            lines.append(
                f"最可能身份：{top_roles[0]}"
                f"（{max_count}/{total}，{max_count / total * 100:.1f}%）"
            )
        else:
            lines.append(
                f"最可能身份：{'、'.join(top_roles)}"
                f"（各 {max_count} 局，并列）"
            )
        profile = self._get_profile(player)
        if profile:
            lines.append(f"玩家画像：{self._profile_line(profile)}")
        lines.append("")
        lines.append("参考基准：")
        lines.append(
            f"· {player} 总体："
            f"{_role_line(player_summary['roles'], player_summary['games'])}"
        )
        lines.append(
            f"· {general} 总体："
            f"{_role_line(general_summary['roles'], general_summary['games'])}"
        )
        lines.append(
            f"· 全体玩家："
            f"{_role_line(global_summary['roles'], global_summary['games'])}"
        )
        sizes = Counter(record["player_count"] for record in combo_records)
        lines.append(
            "· 对局规模："
            + " / ".join(
                f"{size}人×{count}"
                for size, count in sorted(
                    sizes.items(),
                    key=lambda item: (-item[1], item[0]),
                )
            )
        )
        if total < 5:
            lines.append(f"⚠ 样本仅 {total} 局，结论仅供参考。")
        return "\n".join(lines)

    def _recent_text(self, games: list[dict], limit: int) -> str:
        """Build a recent games message."""
        shown = games[:limit]
        lines = [f"【最近 {len(shown)} 局】"]
        result_marks = {"胜利": "胜", "失败": "负", "平局": "平"}
        for index, game in enumerate(shown, 1):
            lines.append(
                f"{index}. {game['date']} {game['time']} · "
                f"{game['player_count']}人局 · "
                f"{game['winner'] or '结果未知'}"
            )
            players = [
                f"{record['player']}({record['general']}/"
                f"{record['role']})"
                f"{result_marks.get(record['result'], '?')}"
                for record in game["players"]
            ]
            lines.append("   " + " ｜ ".join(players))
        return "\n".join(lines)

    @staticmethod
    def _not_found_text(
        kind: str,
        query: str,
        candidates: list[str],
    ) -> str:
        """Build a friendly name-not-found message."""
        if candidates:
            return (
                f"没有找到{kind}「{query}」。"
                f"你是不是想找：{'、'.join(candidates[:10])}？"
            )
        return f"没有找到{kind}「{query}」。发送 /zhanji help 查看用法。"

    def _player_payload(
        self,
        player: str,
        records: list[dict],
        include_affinity: bool = True,
    ) -> dict:
        """Build JSON-friendly player statistics."""
        summary = _summary(records)
        payload = {
            "player": player,
            **summary,
            "role_rates": _role_rates(summary["roles"], summary["games"]),
            "style_metrics": self._style_metrics(records),
        }
        profile = self._get_profile(player)
        if profile:
            payload["profile"] = profile
        if include_affinity:
            by_general = _group_records(records, "general")
            top_generals = []
            for general, items in sorted(
                by_general.items(),
                key=lambda item: (-len(item[1]), item[0]),
            )[:5]:
                item = _summary(items)
                top_generals.append(
                    {
                        "general": general,
                        **item,
                        "role_rates": _role_rates(
                            item["roles"],
                            item["games"],
                        ),
                    }
                )
            payload["top_generals"] = top_generals
        return payload

    def _general_payload(
        self,
        general: str,
        records: list[dict],
        include_affinity: bool = True,
    ) -> dict:
        """Build JSON-friendly general statistics."""
        summary = _summary(records)
        payload = {
            "general": general,
            **summary,
            "role_rates": _role_rates(summary["roles"], summary["games"]),
        }
        if include_affinity:
            by_player = _group_records(records, "player")
            top_players = []
            for player, items in sorted(
                by_player.items(),
                key=lambda item: (-len(item[1]), item[0]),
            )[:5]:
                item = _summary(items)
                top_players.append(
                    {
                        "player": player,
                        **item,
                        "role_rates": _role_rates(
                            item["roles"],
                            item["games"],
                        ),
                    }
                )
            payload["top_players"] = top_players
        return payload

    def _combo_payload(
        self,
        player: str,
        general: str,
        combo_records: list[dict],
        player_records: list[dict],
        general_records: list[dict],
        all_records: list[dict],
    ) -> dict:
        """Build JSON-friendly player-plus-general statistics."""
        combo = _summary(combo_records)
        player_summary = _summary(player_records)
        general_summary = _summary(general_records)
        global_summary = _summary(all_records)
        max_count = max(combo["roles"].values()) if combo["games"] else 0
        top_roles = [
            role
            for role in ROLES
            if max_count > 0 and combo["roles"][role] == max_count
        ]
        return {
            "player": player,
            "general": general,
            **combo,
            "role_rates": _role_rates(combo["roles"], combo["games"]),
            "most_likely_role": (
                top_roles[0] if len(top_roles) == 1 else None
            ),
            "tied_roles": top_roles if len(top_roles) > 1 else [],
            "most_likely_count": max_count,
            "most_likely_rate": (
                round(max_count / combo["games"], 4) if combo["games"] else 0.0
            ),
            "sample_warning": combo["games"] < 5,
            "player_baseline": {
                "games": player_summary["games"],
                "role_rates": _role_rates(
                    player_summary["roles"],
                    player_summary["games"],
                ),
            },
            "general_baseline": {
                "games": general_summary["games"],
                "role_rates": _role_rates(
                    general_summary["roles"],
                    general_summary["games"],
                ),
            },
            "global_baseline": {
                "games": global_summary["games"],
                "role_rates": _role_rates(
                    global_summary["roles"],
                    global_summary["games"],
                ),
            },
            "game_sizes": dict(
                Counter(record["player_count"] for record in combo_records)
            ),
        }

    def _top_players_payload(self, records: list[dict], limit: int) -> list[dict]:
        """Build a leaderboard payload."""
        by_player = _group_records(records, "player")
        rows = []
        for player, items in by_player.items():
            summary = _summary(items)
            rows.append(
                {
                    "player": player,
                    **summary,
                    "role_rates": _role_rates(
                        summary["roles"],
                        summary["games"],
                    ),
                }
            )
        rows.sort(
            key=lambda row: (-row["score"], -row["games"], row["player"])
        )
        return rows[:limit]

    def _top_generals_payload(
        self,
        records: list[dict],
        limit: int,
    ) -> list[dict]:
        """Build a general popularity payload."""
        by_general = _group_records(records, "general")
        rows = []
        for general, items in by_general.items():
            summary = _summary(items)
            rows.append(
                {
                    "general": general,
                    **summary,
                    "role_rates": _role_rates(
                        summary["roles"],
                        summary["games"],
                    ),
                }
            )
        rows.sort(key=lambda row: (-row["games"], row["general"]))
        return rows[:limit]

    @staticmethod
    def _recent_payload(games: list[dict], limit: int) -> list[dict]:
        """Build a recent games payload."""
        return [
            {
                "datetime": f"{game['date']} {game['time']}",
                "mode": game["mode"],
                "player_count": game["player_count"],
                "winner": game["winner"],
                "players": [
                    {
                        "player": record["player"],
                        "general": record["general"],
                        "role": record["role"],
                        "result": record["result"],
                        "score": record["score"],
                    }
                    for record in game["players"]
                ],
            }
            for game in games[:limit]
        ]

    @filter.command("zhanji", alias={"战绩", "sgs"})
    async def zhanji(self, event: AstrMessageEvent):
        """查询三国杀战绩。"""
        query = self._strip_command(event.get_message_str())
        lower = query.casefold()
        if lower in {"help", "帮助", "?", "？", "说明", "用法"}:
            yield event.plain_result(self._help_text())
            return
        if lower in {"刷新", "refresh", "update"}:
            yield event.plain_result("正在重新抓取战绩数据…")
            try:
                games = await self._load_games(
                    force=True,
                    allow_stale=False,
                )
            except Exception as exc:
                logger.exception("Failed to refresh Sanguosha records")
                yield event.plain_result(f"刷新失败：{exc}")
                return
            yield event.plain_result(
                f"刷新成功：{len(games)} 局，最新 {_latest_text(games)}。"
            )
            return

        if not self._cache_fresh():
            yield event.plain_result("正在抓取战绩数据（首次约需 10 秒）…")
        try:
            games = await self._load_games()
        except Exception as exc:
            logger.exception("Failed to load Sanguosha records")
            yield event.plain_result(
                f"抓取战绩失败：{exc}\n"
                "请稍后重试，或检查插件配置中的网页地址。"
            )
            return
        if not games:
            yield event.plain_result("战绩网页暂时没有数据。")
            return
        range_games = self._filter_games(games, self.default_days)
        if not range_games:
            yield event.plain_result(
                f"最近 {self.default_days} 天没有对局。"
            )
            return
        all_records = [
            record for game in games for record in game["players"]
        ]
        records = [
            record for game in range_games for record in game["players"]
        ]
        players = sorted({record["player"] for record in all_records})
        generals = sorted({record["general"] for record in all_records})

        if not query:
            yield event.plain_result(self._summary_text(range_games, records))
            return

        if query.startswith("最近") or lower.startswith("recent"):
            tail = (
                query[2:].strip()
                if query.startswith("最近")
                else query[6:].strip()
            )
            limit = int(tail) if tail.isdigit() else 5
            limit = max(1, min(limit, 10))
            yield event.plain_result(self._recent_text(range_games, limit))
            return

        if query.startswith("玩家"):
            name_query = query[2:].strip(" ：:")
            if not name_query:
                yield event.plain_result(self._help_text())
                return
            name, candidates = _resolve_name(name_query, players)
            if not name:
                yield event.plain_result(
                    self._not_found_text("玩家", name_query, candidates)
                )
                return
            player_records = [
                record for record in records if record["player"] == name
            ]
            if not player_records:
                yield event.plain_result(
                    f"「{name}」在当前统计范围内没有对局。"
                )
                return
            yield event.plain_result(self._player_text(name, player_records))
            return

        if query.startswith("武将"):
            name_query = query[2:].strip(" ：:")
            if not name_query:
                yield event.plain_result(self._help_text())
                return
            name, candidates = _resolve_name(name_query, generals)
            if not name:
                yield event.plain_result(
                    self._not_found_text("武将", name_query, candidates)
                )
                return
            general_records = [
                record for record in records if record["general"] == name
            ]
            if not general_records:
                yield event.plain_result(
                    f"「{name}」在当前统计范围内没有出场。"
                )
                return
            yield event.plain_result(
                self._general_text(name, general_records)
            )
            return

        player_query = ""
        general_query = ""
        combo_match = re.match(
            r"^(.+?)\s*(?:使用|用的|用|玩|拿)\s*(.+)$",
            query,
        )
        if combo_match:
            player_query = combo_match.group(1).strip()
            general_query = combo_match.group(2).strip()
        else:
            parts = query.split()
            if len(parts) == 1:
                exact_player = _exact_name(parts[0], players)
                exact_general = _exact_name(parts[0], generals)
                if exact_player:
                    player_name, player_candidates = exact_player, []
                    general_name, general_candidates = None, []
                elif exact_general:
                    player_name, player_candidates = None, []
                    general_name, general_candidates = exact_general, []
                else:
                    player_name, player_candidates = _resolve_name(
                        parts[0],
                        players,
                    )
                    general_name, general_candidates = _resolve_name(
                        parts[0],
                        generals,
                    )
                if player_name:
                    player_records = [
                        record
                        for record in records
                        if record["player"] == player_name
                    ]
                    if player_records:
                        yield event.plain_result(
                            self._player_text(player_name, player_records)
                        )
                        return
                if general_name:
                    general_records = [
                        record
                        for record in records
                        if record["general"] == general_name
                    ]
                    if general_records:
                        yield event.plain_result(
                            self._general_text(general_name, general_records)
                        )
                        return
                if player_candidates or general_candidates:
                    options = [
                        f"玩家:{name}" for name in player_candidates
                    ] + [
                        f"武将:{name}" for name in general_candidates
                    ]
                    yield event.plain_result(
                        f"「{parts[0]}」匹配到多个对象："
                        f"{'、'.join(options[:10])}。请写得更完整。"
                    )
                    return
                yield event.plain_result(
                    self._not_found_text("玩家或武将", parts[0], [])
                )
                return
            if len(parts) == 2:
                player_query, general_query = parts
            else:
                for name in sorted(players, key=len, reverse=True):
                    prefix = name + " "
                    if query.casefold().startswith(prefix.casefold()):
                        player_query = name
                        general_query = query[len(prefix):].strip()
                        break
                if not player_query:
                    yield event.plain_result(
                        "格式不太对。试试：/zhanji <昵称> <武将>，"
                        "例如 /zhanji ranber 孙权。"
                    )
                    return

        exact_player = _exact_name(player_query, players)
        exact_general = _exact_name(general_query, generals)
        if exact_player and exact_general:
            player_name, player_candidates = exact_player, []
            general_name, general_candidates = exact_general, []
        else:
            player_name, player_candidates = _resolve_name(
                player_query,
                players,
            )
            general_name, general_candidates = _resolve_name(
                general_query,
                generals,
            )
            if not (player_name and general_name):
                reversed_player, _ = _resolve_name(general_query, players)
                reversed_general, _ = _resolve_name(player_query, generals)
                if reversed_player and reversed_general:
                    player_name = reversed_player
                    general_name = reversed_general
                    player_candidates = []
                    general_candidates = []
        if not player_name:
            yield event.plain_result(
                self._not_found_text(
                    "玩家",
                    player_query,
                    player_candidates,
                )
            )
            return
        if not general_name:
            yield event.plain_result(
                self._not_found_text(
                    "武将",
                    general_query,
                    general_candidates,
                )
            )
            return

        player_records = [
            record for record in records if record["player"] == player_name
        ]
        general_records = [
            record for record in records if record["general"] == general_name
        ]
        if not player_records:
            yield event.plain_result(
                f"「{player_name}」在当前统计范围内没有对局。"
            )
            return
        if not general_records:
            yield event.plain_result(
                f"「{general_name}」在当前统计范围内没有出场。"
            )
            return
        combo_records = [
            record
            for record in player_records
            if record["general"] == general_name
        ]
        if not combo_records:
            yield event.plain_result(
                f"没有找到「{player_name}」使用「{general_name}」的对局。\n"
                f"可以发送「/zhanji 玩家 {player_name}」查看他/她的常用武将。"
            )
            return
        yield event.plain_result(
            self._combo_text(
                player_name,
                general_name,
                combo_records,
                player_records,
                general_records,
                records,
            )
        )

    @filter.llm_tool(name="sanguosha_zhanji")
    async def sanguosha_zhanji_tool(
        self,
        event: AstrMessageEvent,
        player: str = "",
        general: str = "",
        days: int = -1,
    ) -> str:
        """查询「群英战报」三国杀战绩数据。

        当用户询问三国杀战绩、玩家胜率、武将胜率、身份分布、最近对局，尤其是“某玩家使用某武将时最可能是什么身份”时，调用此工具获取真实数据后再回答。不要编造战绩。

        返回的 player.profile 是群友维护的主观性格/打法画像，player.style_metrics 是客观统计指标；回答时请区分主观画像和统计事实，不要编造未提供的性格信息。未指定玩家时，players_with_profiles 会列出已维护画像的玩家，需要了解其中某人的性格时可再次调用并指定 player。

        Args:
            player(string): 玩家昵称，可留空；支持模糊匹配。
            general(string): 武将名称，可留空；支持模糊匹配。
            days(number): 只统计最近多少天；0 表示全部历史；不填则使用插件默认范围。
        """
        player = str(player or "").strip()
        general = str(general or "").strip()
        try:
            days = int(days)
        except (TypeError, ValueError):
            days = -1
        try:
            games = await self._load_games()
        except Exception as exc:
            logger.exception("Failed to load Sanguosha records for LLM tool")
            return json.dumps(
                {"error": f"抓取战绩失败：{exc}"},
                ensure_ascii=False,
            )
        if days < 0:
            days = self.default_days
        range_games = self._filter_games(games, days)
        all_records = [
            record for game in games for record in game["players"]
        ]
        range_records = [
            record for game in range_games for record in game["players"]
        ]
        players = sorted({record["player"] for record in all_records})
        generals = sorted({record["general"] for record in all_records})

        player_name = None
        general_name = None
        player_candidates: list[str] = []
        general_candidates: list[str] = []
        if player.strip():
            player_name, player_candidates = _resolve_name(player, players)
        if general.strip():
            general_name, general_candidates = _resolve_name(general, generals)
        if (player.strip() and not player_name) or (
            general.strip() and not general_name
        ):
            error = {
                "error": "无法匹配指定的名称，请使用更完整的昵称或武将名。",
                "player_query": player,
                "general_query": general,
            }
            if player_candidates:
                error["player_candidates"] = player_candidates
            if general_candidates:
                error["general_candidates"] = general_candidates
            return json.dumps(error, ensure_ascii=False)

        global_summary = _summary(range_records)
        payload: dict = {
            "source": self.base_url,
            "data": {
                "total_games": len(games),
                "games_in_range": len(range_games),
                "range": (
                    f"{games[-1]['date']} ~ {games[0]['date']}" if games else ""
                ),
                "filter_range": (
                    f"{range_games[-1]['date']} ~ {range_games[0]['date']}"
                    if range_games
                    else ""
                ),
                "latest_game": _latest_text(games),
                "days": days,
                "player": player_name,
                "general": general_name,
            },
            "global": {
                "games": global_summary["games"],
                "role_counts": global_summary["roles"],
                "role_rates": _role_rates(
                    global_summary["roles"],
                    global_summary["games"],
                ),
            },
        }
        player_records = (
            [
                record
                for record in range_records
                if record["player"] == player_name
            ]
            if player_name
            else []
        )
        general_records = (
            [
                record
                for record in range_records
                if record["general"] == general_name
            ]
            if general_name
            else []
        )
        if player_name:
            payload["player"] = self._player_payload(
                player_name,
                player_records,
                include_affinity=not general_name,
            )
        if general_name:
            payload["general"] = self._general_payload(
                general_name,
                general_records,
                include_affinity=not player_name,
            )
        if player_name and general_name:
            combo_records = [
                record
                for record in player_records
                if record["general"] == general_name
            ]
            payload["combo"] = self._combo_payload(
                player_name,
                general_name,
                combo_records,
                player_records,
                general_records,
                range_records,
            )
        elif not player_name and not general_name:
            payload["top_players"] = self._top_players_payload(
                range_records,
                5,
            )
            payload["top_generals"] = self._top_generals_payload(
                range_records,
                5,
            )
            payload["recent_games"] = self._recent_payload(range_games, 5)
            profile_names = sorted(self._load_profiles())
            if profile_names:
                payload["players_with_profiles"] = profile_names
        if not range_games:
            payload["note"] = "指定时间范围内没有对局。"
        elif (player_name and not player_records) or (
            general_name and not general_records
        ):
            payload["note"] = "指定时间范围内没有符合条件的对局。"
        return json.dumps(payload, ensure_ascii=False)
