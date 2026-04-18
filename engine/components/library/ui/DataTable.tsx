'use client'

import React, { useState, useMemo, useCallback } from 'react'

export type SortDirection = 'asc' | 'desc' | null

export interface Column<T> {
  key: keyof T | string
  title: string
  sortable?: boolean
  width?: string
  align?: 'left' | 'center' | 'right'
  render?: (value: unknown, row: T, index: number) => React.ReactNode
}

export interface DataTableProps<T extends Record<string, unknown>> {
  columns: Column<T>[]
  data: T[]
  loading?: boolean
  emptyMessage?: string
  /** 행 클릭 핸들러 */
  onRowClick?: (row: T) => void
  /** 외부에서 정렬 제어 (controlled) */
  sortConfig?: { col: string; dir: SortDirection }
  onSort?: (col: string, dir: SortDirection) => void
  /** 페이지네이션 */
  pagination?: {
    page: number
    pageSize: number
    total: number
    onChange: (page: number) => void
  }
  rowKey?: (row: T) => string
  className?: string
  caption?: string
}

export function DataTable<T extends Record<string, unknown>>({
  columns,
  data,
  loading = false,
  emptyMessage = '데이터가 없습니다.',
  onRowClick,
  sortConfig: externalSort,
  onSort,
  pagination,
  rowKey,
  className = '',
  caption,
}: DataTableProps<T>) {
  const [internalSort, setInternalSort] = useState<{ col: string; dir: SortDirection }>({ col: '', dir: null })
  const sort = externalSort ?? internalSort

  const handleSort = useCallback((col: string) => {
    const nextDir: SortDirection = sort.col === col
      ? (sort.dir === 'asc' ? 'desc' : sort.dir === 'desc' ? null : 'asc')
      : 'asc'
    if (onSort) {
      onSort(col, nextDir)
    } else {
      setInternalSort({ col, dir: nextDir })
    }
  }, [sort, onSort])

  const sorted = useMemo(() => {
    if (externalSort || !sort.col || !sort.dir) return data
    return [...data].sort((a, b) => {
      const av = a[sort.col as keyof T]
      const bv = b[sort.col as keyof T]
      if (av == null && bv == null) return 0
      if (av == null) return 1
      if (bv == null) return -1
      const cmp = String(av).localeCompare(String(bv), 'ko', { numeric: true })
      return sort.dir === 'asc' ? cmp : -cmp
    })
  }, [data, externalSort, sort])

  const totalPages = pagination ? Math.ceil(pagination.total / pagination.pageSize) : 0

  return (
    <div className={`datatable-wrap ${className}`.trim()}>
      <div className="datatable-scroll" role="region" aria-label={caption || '데이터 테이블'}>
        <table className="datatable" aria-busy={loading}>
          {caption && <caption className="datatable__caption">{caption}</caption>}
          <thead>
            <tr>
              {columns.map(col => (
                <th
                  key={String(col.key)}
                  className={`datatable__th datatable__th--${col.align || 'left'}${col.sortable ? ' datatable__th--sortable' : ''}`}
                  style={col.width ? { width: col.width } : undefined}
                  onClick={col.sortable ? () => handleSort(String(col.key)) : undefined}
                  aria-sort={
                    col.sortable && sort.col === String(col.key)
                      ? sort.dir === 'asc' ? 'ascending'
                      : sort.dir === 'desc' ? 'descending' : 'none'
                      : undefined
                  }
                  tabIndex={col.sortable ? 0 : undefined}
                  onKeyDown={col.sortable ? (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handleSort(String(col.key)) } } : undefined}
                >
                  <span className="datatable__th-inner">
                    {col.title}
                    {col.sortable && (
                      <span className="datatable__sort-icon" aria-hidden="true">
                        {sort.col === String(col.key) && sort.dir === 'asc' ? '↑'
                          : sort.col === String(col.key) && sort.dir === 'desc' ? '↓'
                          : '⇅'}
                      </span>
                    )}
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading ? (
              Array.from({ length: 5 }).map((_, i) => (
                <tr key={i} className="datatable__skeleton-row">
                  {columns.map(col => (
                    <td key={String(col.key)} className="datatable__td">
                      <span className="skeleton-line" />
                    </td>
                  ))}
                </tr>
              ))
            ) : sorted.length === 0 ? (
              <tr>
                <td colSpan={columns.length} className="datatable__empty">
                  <span className="datatable__empty-icon" aria-hidden="true">📭</span>
                  <span>{emptyMessage}</span>
                </td>
              </tr>
            ) : (
              sorted.map((row, i) => {
                const key = rowKey ? rowKey(row) : String(i)
                return (
                  <tr
                    key={key}
                    className={`datatable__row${onRowClick ? ' datatable__row--clickable' : ''}`}
                    onClick={onRowClick ? () => onRowClick(row) : undefined}
                    onKeyDown={onRowClick ? (e) => { if (e.key === 'Enter') onRowClick(row) } : undefined}
                    tabIndex={onRowClick ? 0 : undefined}
                    role={onRowClick ? 'button' : undefined}
                  >
                    {columns.map(col => {
                      const rawVal = row[col.key as keyof T]
                      return (
                        <td key={String(col.key)} className={`datatable__td datatable__td--${col.align || 'left'}`}>
                          {col.render ? col.render(rawVal, row, i) : String(rawVal ?? '')}
                        </td>
                      )
                    })}
                  </tr>
                )
              })
            )}
          </tbody>
        </table>
      </div>

      {pagination && totalPages > 1 && (
        <div className="datatable__pagination">
          <span className="datatable__pagination-info">
            총 {pagination.total}건 · 페이지 {pagination.page}/{totalPages}
          </span>
          <div className="datatable__pagination-btns">
            <button
              className="btn btn--secondary btn--sm"
              disabled={pagination.page <= 1}
              onClick={() => pagination.onChange(pagination.page - 1)}
              aria-label="이전 페이지"
            >‹</button>
            {Array.from({ length: Math.min(5, totalPages) }, (_, k) => {
              const p = Math.max(1, Math.min(pagination.page - 2, totalPages - 4)) + k
              if (p < 1 || p > totalPages) return null
              return (
                <button
                  key={p}
                  className={`btn btn--sm ${p === pagination.page ? 'btn--primary' : 'btn--ghost'}`}
                  onClick={() => pagination.onChange(p)}
                  aria-current={p === pagination.page ? 'page' : undefined}
                >{p}</button>
              )
            })}
            <button
              className="btn btn--secondary btn--sm"
              disabled={pagination.page >= totalPages}
              onClick={() => pagination.onChange(pagination.page + 1)}
              aria-label="다음 페이지"
            >›</button>
          </div>
        </div>
      )}

      <style>{`
        .datatable-wrap { display: flex; flex-direction: column; gap: 16px; }
        .datatable-scroll { overflow-x: auto; border-radius: var(--radius-md, 8px); border: 1px solid var(--color-border, #e2e8f0); }
        .datatable { width: 100%; border-collapse: collapse; font-size: 0.9375rem; }
        .datatable__caption { text-align: left; font-weight: 600; padding: 0 0 8px; color: var(--color-text, #1a1a1a); }
        .datatable__th {
          padding: 12px 16px;
          background: var(--color-surface, #f8fafc);
          color: var(--color-text-muted, #64748b);
          font-size: 0.8125rem;
          font-weight: 600;
          text-transform: uppercase;
          letter-spacing: 0.04em;
          border-bottom: 1px solid var(--color-border, #e2e8f0);
          white-space: nowrap;
        }
        .datatable__th--left { text-align: left; }
        .datatable__th--center { text-align: center; }
        .datatable__th--right { text-align: right; }
        .datatable__th--sortable { cursor: pointer; user-select: none; }
        .datatable__th--sortable:hover { background: var(--color-border, #e2e8f0); }
        .datatable__th--sortable:focus-visible { outline: 2px solid var(--color-primary, #3b82f6); outline-offset: -2px; }
        .datatable__th-inner { display: flex; align-items: center; gap: 6px; }
        .datatable__sort-icon { color: var(--color-text-muted, #64748b); font-size: 0.75rem; }
        .datatable__td {
          padding: 13px 16px;
          color: var(--color-text, #1a1a1a);
          border-bottom: 1px solid var(--color-border, #e2e8f0);
          vertical-align: middle;
        }
        .datatable__td--center { text-align: center; }
        .datatable__td--right { text-align: right; }
        .datatable__row:last-child .datatable__td { border-bottom: none; }
        .datatable__row--clickable { cursor: pointer; transition: background 0.12s; }
        .datatable__row--clickable:hover { background: var(--color-surface, #f8fafc); }
        .datatable__row--clickable:focus-visible { outline: 2px solid var(--color-primary, #3b82f6); outline-offset: -2px; }
        .datatable__empty { text-align: center; padding: 48px; color: var(--color-text-muted, #64748b); }
        .datatable__empty-icon { display: block; font-size: 2rem; margin-bottom: 8px; }
        .datatable__skeleton-row .skeleton-line {
          display: block; height: 16px; border-radius: 4px;
          background: linear-gradient(90deg, var(--color-surface, #f1f5f9) 25%, var(--color-border, #e2e8f0) 50%, var(--color-surface, #f1f5f9) 75%);
          background-size: 200% 100%;
          animation: skeleton-shimmer 1.4s ease infinite;
        }
        @keyframes skeleton-shimmer { to { background-position: -200% 0; } }
        .datatable__pagination {
          display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap;
        }
        .datatable__pagination-info { font-size: 0.875rem; color: var(--color-text-muted, #64748b); }
        .datatable__pagination-btns { display: flex; gap: 4px; }
        @media (prefers-reduced-motion: reduce) { .skeleton-line { animation: none; } }
      `}</style>
    </div>
  )
}
