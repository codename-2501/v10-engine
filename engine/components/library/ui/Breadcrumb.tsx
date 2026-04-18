'use client'

import React from 'react'

export interface BreadcrumbItem {
  label: string
  href?: string
}

export interface BreadcrumbProps {
  items: BreadcrumbItem[]
  separator?: React.ReactNode
  className?: string
}

export const Breadcrumb = React.memo(function Breadcrumb({
  items,
  separator = '/',
  className = '',
}: BreadcrumbProps) {
  return (
    <nav aria-label="경로" className={`breadcrumb ${className}`.trim()}>
      <ol className="breadcrumb__list">
        {items.map((item, i) => {
          const isLast = i === items.length - 1
          return (
            <li key={i} className="breadcrumb__item">
              {i > 0 && (
                <span className="breadcrumb__sep" aria-hidden="true">{separator}</span>
              )}
              {isLast || !item.href ? (
                <span
                  className="breadcrumb__text"
                  aria-current={isLast ? 'page' : undefined}
                >
                  {item.label}
                </span>
              ) : (
                <a href={item.href} className="breadcrumb__link">{item.label}</a>
              )}
            </li>
          )
        })}
      </ol>

      <style>{`
        .breadcrumb { }
        .breadcrumb__list { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; list-style: none; padding: 0; margin: 0; }
        .breadcrumb__item { display: flex; align-items: center; gap: 6px; }
        .breadcrumb__sep { color: var(--color-text-muted, #64748b); font-size: 0.75rem; user-select: none; }
        .breadcrumb__link {
          font-size: 0.875rem; color: var(--color-primary, #3b82f6);
          text-decoration: none; transition: color 0.15s;
        }
        .breadcrumb__link:hover { color: var(--color-primary-hover, #2563eb); text-decoration: underline; }
        .breadcrumb__link:focus-visible { outline: 2px solid var(--color-primary, #3b82f6); outline-offset: 2px; border-radius: 3px; }
        .breadcrumb__text { font-size: 0.875rem; color: var(--color-text-muted, #64748b); }
        [aria-current="page"] { color: var(--color-text, #1a1a1a); font-weight: 500; }
      `}</style>
    </nav>
  )
})
