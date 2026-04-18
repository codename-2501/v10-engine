'use client'

import React from 'react'

export interface MobileNavItem {
  key: string
  label: string
  href: string
  icon: React.ReactNode
  badge?: number
}

export interface MobileBottomNavProps {
  items: MobileNavItem[]
  activeKey?: string
  className?: string
}

export const MobileBottomNav = React.memo(function MobileBottomNav({
  items,
  activeKey,
  className = '',
}: MobileBottomNavProps) {
  return (
    <nav
      className={`mobile-bottom-nav ${className}`.trim()}
      aria-label="하단 탭 메뉴"
    >
      {items.map(item => {
        const isActive = activeKey === item.key
        return (
          <a
            key={item.key}
            href={item.href}
            className={`mobile-bottom-nav__item${isActive ? ' mobile-bottom-nav__item--active' : ''}`}
            aria-current={isActive ? 'page' : undefined}
            aria-label={item.badge ? `${item.label} (${item.badge}건)` : item.label}
          >
            <span className="mobile-bottom-nav__icon-wrap">
              <span className="mobile-bottom-nav__icon" aria-hidden="true">{item.icon}</span>
              {item.badge != null && item.badge > 0 && (
                <span className="mobile-bottom-nav__badge">{item.badge > 99 ? '99+' : item.badge}</span>
              )}
            </span>
            <span className="mobile-bottom-nav__label">{item.label}</span>
          </a>
        )
      })}

      <style>{`
        .mobile-bottom-nav {
          display: none;
          position: fixed; bottom: 0; left: 0; right: 0;
          background: var(--color-surface, #fff);
          border-top: 1px solid var(--color-border, #e2e8f0);
          z-index: 200;
          padding-bottom: env(safe-area-inset-bottom, 0px);
        }
        @media (max-width: 768px) {
          .mobile-bottom-nav { display: flex; }
        }
        .mobile-bottom-nav__item {
          flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center;
          gap: 3px; padding: 10px 8px;
          color: var(--color-text-muted, #94a3b8);
          text-decoration: none;
          transition: color 0.15s;
        }
        .mobile-bottom-nav__item--active { color: var(--color-primary, #3b82f6); }
        .mobile-bottom-nav__item:focus-visible { outline: 2px solid var(--color-primary, #3b82f6); outline-offset: -2px; border-radius: 4px; }
        .mobile-bottom-nav__icon-wrap { position: relative; display: flex; }
        .mobile-bottom-nav__icon { font-size: 1.375rem; display: flex; }
        .mobile-bottom-nav__badge {
          position: absolute; top: -4px; right: -8px;
          min-width: 16px; height: 16px; padding: 0 4px;
          background: var(--color-danger, #ef4444); color: #fff;
          font-size: 0.6rem; font-weight: 700;
          border-radius: 999px;
          display: flex; align-items: center; justify-content: center;
        }
        .mobile-bottom-nav__label { font-size: 0.6875rem; font-weight: 500; white-space: nowrap; }
        @media (prefers-reduced-motion: reduce) { .mobile-bottom-nav__item { transition: none; } }
      `}</style>
    </nav>
  )
})
