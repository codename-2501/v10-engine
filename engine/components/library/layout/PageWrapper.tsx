'use client'

import React from 'react'

export interface PageWrapperProps {
  title?: string
  description?: string
  actions?: React.ReactNode
  breadcrumb?: React.ReactNode
  children: React.ReactNode
  maxWidth?: string
  padding?: 'none' | 'sm' | 'md' | 'lg'
  className?: string
  /** 사이드바 레이아웃 포함 여부 */
  sidebar?: React.ReactNode
  /** 고정 하단 영역 (모바일 바텀 탭) */
  mobileBottomNav?: React.ReactNode
}

export const PageWrapper = React.memo(function PageWrapper({
  title,
  description,
  actions,
  breadcrumb,
  children,
  maxWidth = '1280px',
  padding = 'lg',
  className = '',
  sidebar,
  mobileBottomNav,
}: PageWrapperProps) {
  const padMap = { none: '0', sm: 'var(--space-md, 16px)', md: 'var(--space-lg, 24px)', lg: 'var(--space-xl, 32px)' }
  const padValue = padMap[padding]

  return (
    <div
      className={`page-wrapper${sidebar ? ' page-wrapper--with-sidebar' : ''} ${className}`.trim()}
      style={{ '--page-pad': padValue } as React.CSSProperties}
    >
      {sidebar && <div className="page-wrapper__sidebar">{sidebar}</div>}
      <div className="page-wrapper__main">
        <div
          className="page-wrapper__content"
          style={{ maxWidth }}
        >
          {breadcrumb && <div className="page-wrapper__breadcrumb">{breadcrumb}</div>}
          {(title || actions) && (
            <div className="page-wrapper__header">
              <div className="page-wrapper__header-left">
                {title && <h1 className="page-wrapper__title">{title}</h1>}
                {description && <p className="page-wrapper__desc">{description}</p>}
              </div>
              {actions && <div className="page-wrapper__header-actions">{actions}</div>}
            </div>
          )}
          {children}
        </div>
      </div>
      {mobileBottomNav}

      <style>{`
        .page-wrapper {
          display: flex;
          min-height: calc(100vh - var(--header-h, 60px));
          background: var(--color-bg, #f8fafc);
        }
        .page-wrapper--with-sidebar { }
        .page-wrapper__sidebar { flex-shrink: 0; }
        .page-wrapper__main { flex: 1; overflow: hidden; }
        .page-wrapper__content {
          margin: 0 auto;
          padding: var(--page-pad, var(--space-xl, 32px));
          display: flex; flex-direction: column; gap: var(--space-lg, 24px);
        }
        .page-wrapper__breadcrumb { }
        .page-wrapper__header {
          display: flex; align-items: flex-start; justify-content: space-between;
          gap: 16px; flex-wrap: wrap;
        }
        .page-wrapper__header-left { flex: 1; min-width: 0; }
        .page-wrapper__header-actions { flex-shrink: 0; display: flex; gap: 8px; flex-wrap: wrap; }
        .page-wrapper__title {
          font-size: 1.5rem; font-weight: 700;
          color: var(--color-text, #1a1a1a); margin: 0;
        }
        .page-wrapper__desc {
          font-size: 0.9375rem; color: var(--color-text-muted, #64748b);
          margin: 6px 0 0;
        }
        @media (max-width: 768px) {
          .page-wrapper__content {
            padding: var(--space-md, 16px);
            padding-bottom: calc(var(--space-md, 16px) + 64px);
          }
          .page-wrapper__title { font-size: 1.25rem; }
          .page-wrapper__header { flex-direction: column; }
          .page-wrapper__header-actions { width: 100%; }
        }
      `}</style>
    </div>
  )
})
