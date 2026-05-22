import math
from html import escape as html_escape
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import verify_token
from app.core.exceptions import APIError
from app.models.models import Board, BoardElement
from app.schemas.common import CamelModel

router = APIRouter(prefix="/frames", tags=["frames"])


class FrameMeta(CamelModel):
    id: UUID
    title: str
    w: float
    h: float
    attrs: dict


class BoardMeta(CamelModel):
    id: UUID
    title: str


class FrameChild(CamelModel):
    id: UUID
    type: str
    parent_id: UUID | None
    z_index: int
    rel_x: float
    rel_y: float
    w: float
    h: float
    attrs: dict


class FrameView(CamelModel):
    frame: FrameMeta
    board: BoardMeta
    children: list[FrameChild]


async def _load_frame_data(db: AsyncSession, frame_id: UUID) -> dict:
    frame = await db.get(BoardElement, frame_id)
    if not frame or frame.deleted_at is not None:
        raise APIError(404, "frame_not_found", f"Frame '{frame_id}' does not exist")
    if frame.type != "frame":
        raise APIError(400, "not_a_frame", f"Element '{frame_id}' is not a frame")

    board = await db.get(Board, frame.board_id)
    if not board or board.deleted_at is not None:
        raise APIError(404, "board_not_found", f"Board for frame '{frame_id}' is unavailable")

    q = (
        select(BoardElement)
        .where(
            BoardElement.parent_id == frame_id,
            BoardElement.deleted_at.is_(None),
        )
        .order_by(BoardElement.z_index.asc())
    )
    children = (await db.execute(q)).scalars().all()

    return {
        "frame": {
            "id": frame.id,
            "title": (frame.attrs or {}).get("title", "") or "",
            "w": frame.w,
            "h": frame.h,
            "attrs": frame.attrs or {},
        },
        "board": {"id": board.id, "title": board.title},
        "children": [
            {
                "id": c.id,
                "type": c.type,
                "parentId": c.parent_id,
                "zIndex": c.z_index,
                "relX": c.x - frame.x,
                "relY": c.y - frame.y,
                "w": c.w,
                "h": c.h,
                "attrs": c.attrs or {},
            }
            for c in children
        ],
    }


@router.get("/{frame_id}.html", response_class=HTMLResponse)
async def get_frame_html(
    frame_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    data = await _load_frame_data(db, frame_id)
    body = render_frame_as_html(data)
    title = html_escape(data["frame"].get("title") or "Без названия")
    page = (
        '<!doctype html>\n'
        '<html lang="ru"><head><meta charset="utf-8">'
        f'<title>{title}</title>'
        '<style>body{margin:0;padding:24px;background:#f8f9fa;'
        'font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}'
        '</style>'
        '</head><body>'
        f'{body}'
        '</body></html>'
    )
    return HTMLResponse(content=page, headers={"Cache-Control": "no-store"})


@router.get("/{frame_id}.png")
async def get_frame_png(
    frame_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> Response:
    import cairosvg

    data = await _load_frame_data(db, frame_id)
    svg = render_frame_as_svg(data)
    png_bytes = cairosvg.svg2png(bytestring=svg.encode("utf-8"))
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/{frame_id}", response_model=FrameView)
async def get_frame(
    frame_id: UUID,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_token),
) -> dict:
    data = await _load_frame_data(db, frame_id)
    # Внутренний формат _load_frame_data — camelCase, FrameView ожидает snake_case через alias.
    # Для совместимости с FrameView переименуем поля обратно.
    return {
        "frame": data["frame"],
        "board": data["board"],
        "children": [
            {
                "id": c["id"],
                "type": c["type"],
                "parent_id": c["parentId"],
                "z_index": c["zIndex"],
                "rel_x": c["relX"],
                "rel_y": c["relY"],
                "w": c["w"],
                "h": c["h"],
                "attrs": c["attrs"],
            }
            for c in data["children"]
        ],
    }


# ── Public render endpoints (без auth) ────────────────────────────────────────

def _fill_color(a: dict) -> str:
    if "fill" in a:
        v = a["fill"]
        return "transparent" if v is None else (v or "#ffffff")
    return "#ffffff"


def _stroke_color(a: dict) -> str:
    if "stroke" in a:
        v = a["stroke"]
        return "transparent" if v is None else (v or "#212529")
    return "#212529"


def _svg_fill_color(a: dict) -> str:
    if "fill" in a:
        v = a["fill"]
        return "none" if v is None else (v or "#ffffff")
    return "#ffffff"


def _svg_stroke_color(a: dict) -> str:
    if "stroke" in a:
        v = a["stroke"]
        return "none" if v is None else (v or "#212529")
    return "#212529"


def render_frame_as_html(data: dict) -> str:
    f = data["frame"]
    title = f.get("title") or ""
    board_title = data["board"]["title"]
    parts: list[str] = [
        f"<!-- {html_escape(title or 'Без названия')} ({html_escape(board_title)}) -->",
        f'<div style="position: relative; width: {f["w"]}px; height: {f["h"]}px; box-sizing: border-box;">',
    ]
    for c in data["children"]:
        a = c.get("attrs") or {}
        rx, ry, w, h = c["relX"], c["relY"], c["w"], c["h"]
        if c["type"] == "rect":
            fill = _fill_color(a)
            stroke = _stroke_color(a)
            fo = a.get("fillOpacity", 1)
            so = a.get("strokeOpacity", 1)
            opacity = "" if fo == 1 and so == 1 else f"; opacity: {min(fo, so)}"
            sw = a.get("strokeWidth", 2)
            rad = a.get("rx", 4)
            parts.append(
                f'  <div style="position: absolute; left: {rx}px; top: {ry}px; '
                f'width: {w}px; height: {h}px; background: {fill}; '
                f'border: {sw}px solid {stroke}; border-radius: {rad}px; '
                f'box-sizing: border-box{opacity}"></div>'
            )
        elif c["type"] == "text":
            font_size = a.get("fontSize") or 14
            color = a.get("color") or "#212529"
            styles = [
                "position: absolute",
                f"left: {rx}px",
                f"top: {ry}px",
                f"font-size: {font_size}px",
                f"color: {color}",
                'font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif',
            ]
            if a.get("bold"):
                styles.append("font-weight: 700")
            if a.get("italic"):
                styles.append("font-style: italic")
            if a.get("underline"):
                styles.append("text-decoration: underline")
            parts.append(
                f'  <div style="{"; ".join(styles)}">{html_escape(a.get("text") or "")}</div>'
            )
        elif c["type"] == "note":
            parts.append(
                f'  <div style="position: absolute; left: {rx}px; top: {ry}px; '
                f'width: {w}px; height: {h}px; background: #fff8c6; '
                f'border: 1px solid #f1c40f; border-radius: 2px; padding: 8px; '
                f'box-sizing: border-box; font-size: 13px; line-height: 1.4; '
                f'white-space: pre-wrap; font-family: inherit;">'
                f'{html_escape(a.get("text") or "")}</div>'
            )
        elif c["type"] == "line":
            length = math.hypot(w, h)
            angle = math.degrees(math.atan2(h, w))
            parts.append(
                f'  <div style="position: absolute; left: {rx}px; top: {ry}px; '
                f'width: {length}px; height: 2px; background: #212529; '
                f'transform-origin: 0 50%; transform: rotate({angle:.2f}deg);"></div>'
            )
    parts.append("</div>")
    return "\n".join(parts)


def render_frame_as_svg(data: dict) -> str:
    f = data["frame"]
    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{f["w"]}" height="{f["h"]}" '
        f'viewBox="0 0 {f["w"]} {f["h"]}">',
        f'<rect x="0" y="0" width="{f["w"]}" height="{f["h"]}" fill="#ffffff"/>',
    ]
    for c in data["children"]:
        a = c.get("attrs") or {}
        rx, ry, w, h = c["relX"], c["relY"], c["w"], c["h"]
        if c["type"] == "rect":
            fill = _svg_fill_color(a)
            stroke = _svg_stroke_color(a)
            fo = (
                f' fill-opacity="{a["fillOpacity"]}"' if "fillOpacity" in a else ""
            )
            so = (
                f' stroke-opacity="{a["strokeOpacity"]}"' if "strokeOpacity" in a else ""
            )
            sw = a.get("strokeWidth", 2)
            rad = a.get("rx", 4)
            out.append(
                f'<rect x="{rx}" y="{ry}" width="{w}" height="{h}" '
                f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}" rx="{rad}"{fo}{so}/>'
            )
        elif c["type"] == "text":
            font_size = a.get("fontSize") or 14
            color = a.get("color") or "#212529"
            y = ry + font_size
            weight = "bold" if a.get("bold") else "normal"
            style = "italic" if a.get("italic") else "normal"
            deco = "underline" if a.get("underline") else "none"
            out.append(
                f'<text x="{rx}" y="{y}" font-size="{font_size}" fill="{color}" '
                f'font-weight="{weight}" font-style="{style}" '
                f'text-decoration="{deco}" '
                f'font-family="system-ui, -apple-system, Segoe UI, Roboto, sans-serif">'
                f'{html_escape(a.get("text") or "")}</text>'
            )
        elif c["type"] == "note":
            out.append(
                f'<rect x="{rx}" y="{ry}" width="{w}" height="{h}" '
                f'fill="#fff8c6" stroke="#f1c40f" stroke-width="1" rx="2"/>'
            )
            font_size = 13
            padding = 8
            line_h = font_size * 1.4
            for i, line in enumerate((a.get("text") or "").split("\n")):
                y = ry + padding + font_size + i * line_h
                out.append(
                    f'<text x="{rx + padding}" y="{y}" font-size="{font_size}" '
                    f'fill="#212529" font-family="system-ui, sans-serif">'
                    f'{html_escape(line)}</text>'
                )
        elif c["type"] == "line":
            out.append(
                f'<line x1="{rx}" y1="{ry}" x2="{rx + w}" y2="{ry + h}" '
                f'stroke="#212529" stroke-width="2" stroke-linecap="round"/>'
            )
    out.append("</svg>")
    return "".join(out)
