"""Public pages API — serves published HTML without authentication."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.published_page import PublishedPage
from app.models.user import User
from app.services.storage import get_storage_backend, normalize_storage_key

# Public router — no /api prefix, no auth
public_router = APIRouter(tags=["pages"])

# Authenticated router — under /api prefix
router = APIRouter(prefix="/pages", tags=["pages"])

# ── Public render (NO auth) ────────────────────────────

@public_router.get("/p/{short_id}")
async def render_page(short_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Serve a published HTML page. No authentication required."""
    # 0. IP 粒度速率限制：防止恶意客户端高频请求公开页面消耗资源
    from app.core.rate_limit import check_ip_rate_limit
    client_ip = request.client.host if request.client else "0.0.0.0"
    await check_ip_rate_limit(client_ip, "pages_render", {"pages_render": (30, 60)})

    result = await db.execute(
        select(PublishedPage).where(PublishedPage.short_id == short_id)
    )
    page = result.scalar_one_or_none()
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")

    storage = get_storage_backend()
    storage_key = normalize_storage_key(f"{page.agent_id}/{page.source_path}")
    if not await storage.exists(storage_key) or not await storage.is_file(storage_key):
        raise HTTPException(status_code=404, detail="Source file no longer exists")

    html_content = await storage.read_text(storage_key, encoding="utf-8", errors="replace")

    # 服务端 HTML 净化：移除危险标签和属性，配合 CSP sandbox 形成深度防御
    import bleach
    html_content = bleach.clean(
        html_content,
        tags=bleach.ALLOWED_TAGS | {'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'div', 'span',
                                     'pre', 'code', 'table', 'thead', 'tbody', 'tr', 'th', 'td',
                                     'img', 'svg', 'path', 'circle', 'rect', 'line', 'polyline',
                                     'g', 'defs', 'use', 'marker', 'pattern', 'text', 'tspan'},
        attributes={'*': ['class', 'id', 'style', 'data-*'],
                    'img': ['src', 'alt', 'width', 'height'],
                    'a': ['href', 'target', 'rel'],
                    'svg': ['xmlns', 'viewBox', 'width', 'height', 'fill', 'stroke'],
                    'path': ['d', 'fill', 'stroke', 'stroke-width'],
                    'use': ['href', 'x', 'y', 'width', 'height']},
        strip=True,
    )

    # Increment view count
    await db.execute(
        update(PublishedPage)
        .where(PublishedPage.id == page.id)
        .values(view_count=PublishedPage.view_count + 1)
    )
    await db.commit()

    return HTMLResponse(
        content=html_content,
        headers={
            # CSP sandbox: isolates origin, prevents access to parent localStorage/cookies
            "Content-Security-Policy": "sandbox allow-scripts allow-forms allow-popups allow-modals",
            "X-Content-Type-Options": "nosniff",
        },
    )


# ── Authenticated endpoints ────────────────────────────

@router.get("/list")
async def list_pages(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List published pages for an agent."""
    from app.core.permissions import check_agent_access
    await check_agent_access(db, current_user, agent_id)

    result = await db.execute(
        select(PublishedPage)
        .where(PublishedPage.agent_id == agent_id)
        .order_by(PublishedPage.created_at.desc())
    )
    pages = result.scalars().all()
    return [
        {
            "id": str(p.id),
            "short_id": p.short_id,
            "source_path": p.source_path,
            "title": p.title,
            "view_count": p.view_count,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "url": f"/p/{p.short_id}",
        }
        for p in pages
    ]
