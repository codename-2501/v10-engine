'use client'

import React, { useState } from 'react'

export interface NavItem {
  label: string
  href: string
  active?: boolean
  icon?: React.ReactNode
}

export interface HeaderProps {
  logo?: React.ReactNode
  logoText?: string
  nav?: NavItem[]
  actions?: React.ReactNode
  onMenuToggle?: () => void
  sidebarOpen?: boolean
  sticky?: boolean
  className?: string
}

export const Header = React.memo(function Header({
  logo,
  logoText = 'App',
  nav = [],
  actions,
  onMenuToggle,
  sidebarOpen,
  sticky = true,
  className = '',
}: HeaderProps) {
  const [mobileNavOpen, setMobileNavOpen] = useState(false)

  return (
    <header className={`header${sticky ? ' header--sticky' : ''} ${className}`.trim()}>
      <div className="header__inner">
        <div className="header__left">
          {onMenuToggle && (
            <button
              className="header__menu-btn"
              onClick={onMenuToggle}
              aria-label={sidebarOpen ? '사이드바 닫기' : '사이드바 열기'}
              aria-expanded={sidebarOpen}
              type="button"
            >
              <svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true">
                {sidebarOpen
                  ? <path d="M3 5h14M3 10h14M3 15h14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
                  : <path d="M3 5h14M3 10h14M3 15h14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
                }
              </svg>
            </button>
          )}
          <a href="/" className="header__logo" aria-label="홈으로">
            {logo || <span className="header__logo-text">{logoText}</span>}
          </a>
          {nav.length > 0 && (
            <nav className="header__nav" aria-label="주요 메뉴">
              {nav.map(item => (
                <a
                  key={item.href}
                  href={item.href}
                  className={`header__nav-link${item.active ? ' header__nav-link--active' : ''}`}
                  aria-current={item.active ? 'page' : undefined}
                >
                  {item.icon && <span className="header__nav-icon" aria-hidden="true">{item.icon}</span>}
                  {item.label}
                </a>
              ))}
            </nav>
          )}
        </div>

        <div className="header__right">
          {actions && <div className="header__actions">{actions}</div>}
          {nav.length > 0 && (
            <button
              className="header__mobile-toggle"
              onClick={() => setMobileNavOpen(v => !v)}
              aria-label="모바일 메뉴"
              aria-expanded={mobileNavOpen}
              type="button"
            >
              <svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true">
                <path d="M3 5h14M3 10h14M3 15h14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
              </svg>
            </button>
          )}
        </div>
      </div>

      {mobileNavOpen && nav.length > 0 && (
        <nav className="header__mobile-nav" aria-label="모바일 메뉴">
          {nav.map(item => (
            <a
              key={item.href}
              href={item.href}
              className={`header__mobile-link${item.active ? ' header__mobile-link--active' : ''}`}
              aria-current={item.active ? 'page' : undefined}
              onClick={() => setMobileNavOpen(false)}
            >
              {item.icon && <span aria-hidden="true">{item.icon}</span>}
              {item.label}
            </a>
          ))}
        </nav>
      )}

      <style>{`
        .header {
          height: var(--header-h, 60px);
          background: var(--color-surface, #fff);
          border-bottom: 1px solid var(--color-border, #e2e8f0);
          z-index: 100;
          width: 100%;
        }
        .header--sticky { position: sticky; top: 0; }
        .header__inner {
          display: flex; align-items: center; justify-content: space-between;
          height: 100%;
          padding: 0 var(--space-lg, 24px);
          max-width: var(--max-w, 1280px);
          margin: 0 auto;
          gap: 16px;
        }
        .header__left { display: flex; align-items: center; gap: 16px; min-width: 0; }
        .header__right { display: flex; align-items: center; gap: 12px; flex-shrink: 0; }
        .header__logo { display: flex; align-items: center; text-decoration: none; flex-shrink: 0; }
        .header__logo-text {
          font-size: 1.125rem; font-weight: 700;
          color: var(--color-primary, #3b82f6);
          letter-spacing: -0.02em;
        }
        .header__menu-btn {
          display: flex; align-items: center; justify-content: center;
          width: 36px; height: 36px;
          background: transparent; border: none; border-radius: var(--radius-sm, 6px);
          cursor: pointer; color: var(--color-text-muted, #64748b);
          transition: background 0.15s;
        }
        .header__menu-btn:hover { background: var(--color-surface, #f1f5f9); }
        .header__menu-btn:focus-visible { outline: 2px solid var(--color-primary, #3b82f6); outline-offset: 2px; }
        .header__nav { display: flex; align-items: center; gap: 4px; }
        .header__nav-link {
          display: flex; align-items: center; gap: 6px;
          padding: 6px 12px;
          font-size: 0.9375rem; font-weight: 500;
          color: var(--color-text-muted, #64748b);
          text-decoration: none;
          border-radius: var(--radius-sm, 6px);
          transition: background 0.15s, color 0.15s;
          white-space: nowrap;
        }
        .header__nav-link:hover { background: var(--color-surface, #f1f5f9); color: var(--color-text, #1a1a1a); }
        .header__nav-link--active { color: var(--color-primary, #3b82f6); background: color-mix(in srgb, var(--color-primary, #3b82f6) 10%, transparent); }
        .header__nav-link:focus-visible { outline: 2px solid var(--color-primary, #3b82f6); outline-offset: 2px; }
        .header__actions { display: flex; align-items: center; gap: 8px; }
        .header__mobile-toggle {
          display: none; align-items: center; justify-content: center;
          width: 36px; height: 36px;
          background: transparent; border: none; border-radius: 6px;
          cursor: pointer; color: var(--color-text-muted, #64748b);
        }
        .header__mobile-nav {
          display: flex; flex-direction: column;
          background: var(--color-surface, #fff);
          border-top: 1px solid var(--color-border, #e2e8f0);
          padding: 8px var(--space-md, 16px);
          gap: 4px;
        }
        .header__mobile-link {
          display: flex; align-items: center; gap: 10px;
          padding: 10px 12px;
          font-size: 0.9375rem; font-weight: 500;
          color: var(--color-text-muted, #64748b);
          text-decoration: none; border-radius: 6px;
          transition: background 0.15s, color 0.15s;
        }
        .header__mobile-link:hover, .header__mobile-link--active {
          background: var(--color-surface, #f1f5f9); color: var(--color-primary, #3b82f6);
        }
        @media (max-width: 768px) {
          .header__nav { display: none; }
          .header__mobile-toggle { display: flex; }
        }
        @media (prefers-reduced-motion: reduce) {
          .header__nav-link, .header__menu-btn { transition: none; }
        }
      `}</style>
    </header>
  )
})
