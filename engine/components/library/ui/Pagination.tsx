'use client'

import React, { useCallback } from 'react'

export interface PaginationProps {
  page: number
  pageSize: number
  total: number
  onChange: (page: number) => void
  maxVisible?: number
  showInfo?: boolean
  className?: string
}

export const Pagination = React.memo(function Pagination({
  page,
  pageSize,
  total,
  onChange,
  maxVisible = 7,
  showInfo = true,
  className = '',
}: PaginationProps) {
  const totalPages = Math.ceil(total / pageSize)

  const getPages = useCallback((): (number | 'ellipsis')[] => {
    if (totalPages <= maxVisible) {
      return Array.from({ length: totalPages }, (_, i) => i + 1)
    }
    const half = Math.floor(maxVisible / 2)
    let start = Math.max(1, page - half)
    let end = Math.min(totalPages, start + maxVisible - 1)
    if (end - start < maxVisible - 1) start = Math.max(1, end - maxVisible + 1)

    const pages: (number | 'ellipsis')[] = []
    if (start > 1) { pages.push(1); if (start > 2) pages.push('ellipsis') }
    for (let i = start; i <= end; i++) pages.push(i)
    if (end < totalPages) { if (end < totalPages - 1) pages.push('ellipsis'); pages.push(totalPages) }
    return pages
  }, [page, totalPages, maxVisible])

  if (totalPages <= 1) return null

  const start = (page - 1) * pageSize + 1
  const end = Math.min(page * pageSize, total)

  return (
    <nav className={`pagination ${className}`.trim()} aria-label="페이지네이션">
      {showInfo && (
        <span className="pagination__info" aria-live="polite">
          {total.toLocaleString()}건 중 {start.toLocaleString()}–{end.toLocaleString()}
        </span>
      )}
      <div className="pagination__controls">
        <button
          className="pagination__btn pagination__btn--nav"
          disabled={page <= 1}
          onClick={() => onChange(page - 1)}
          aria-label="이전 페이지"
          type="button"
        >
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
            <path d="M9 2L4 7l5 5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
          </svg>
        </button>

        {getPages().map((p, i) =>
          p === 'ellipsis' ? (
            <span key={`e${i}`} className="pagination__ellipsis" aria-hidden="true">…</span>
          ) : (
            <button
              key={p}
              className={`pagination__btn${p === page ? ' pagination__btn--active' : ''}`}
              onClick={() => onChange(p)}
              aria-current={p === page ? 'page' : undefined}
              aria-label={`${p}페이지`}
              type="button"
            >{p}</button>
          )
        )}

        <button
          className="pagination__btn pagination__btn--nav"
          disabled={page >= totalPages}
          onClick={() => onChange(page + 1)}
          aria-label="다음 페이지"
          type="button"
        >
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
            <path d="M5 2l5 5-5 5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
          </svg>
        </button>
      </div>

      <style>{`
        .pagination { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
        .pagination__info { font-size: 0.875rem; color: var(--color-text-muted, #64748b); }
        .pagination__controls { display: flex; align-items: center; gap: 4px; }
        .pagination__btn {
          display: inline-flex; align-items: center; justify-content: center;
          min-width: 34px; height: 34px; padding: 0 6px;
          font-size: 0.875rem; font-weight: 500; font-family: inherit;
          border: 1px solid var(--color-border, #e2e8f0);
          border-radius: var(--radius-sm, 4px);
          background: var(--color-surface, #fff);
          color: var(--color-text, #1a1a1a);
          cursor: pointer;
          transition: background 0.12s, border-color 0.12s, color 0.12s;
        }
        .pagination__btn:hover:not(:disabled) {
          background: var(--color-surface, #f1f5f9);
          border-color: var(--color-primary, #3b82f6);
        }
        .pagination__btn--active {
          background: var(--color-primary, #3b82f6);
          border-color: var(--color-primary, #3b82f6);
          color: #fff;
        }
        .pagination__btn--active:hover { background: var(--color-primary-hover, #2563eb); }
        .pagination__btn:disabled { opacity: 0.4; cursor: not-allowed; }
        .pagination__btn:focus-visible { outline: 2px solid var(--color-primary, #3b82f6); outline-offset: 2px; }
        .pagination__ellipsis { padding: 0 4px; color: var(--color-text-muted, #64748b); user-select: none; }
        @media (max-width: 480px) {
          .pagination { justify-content: center; }
          .pagination__info { display: none; }
        }
      `}</style>
    </nav>
  )
})
