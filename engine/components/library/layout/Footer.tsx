'use client'

import React from 'react'

export interface FooterLink {
  label: string
  href: string
  external?: boolean
}

export interface FooterProps {
  logo?: React.ReactNode
  logoText?: string
  copyright?: string
  links?: FooterLink[]
  extra?: React.ReactNode
  compact?: boolean
  className?: string
}

export const Footer = React.memo(function Footer({
  logo,
  logoText,
  copyright,
  links = [],
  extra,
  compact = false,
  className = '',
}: FooterProps) {
  const year = new Date().getFullYear()

  return (
    <footer className={`footer${compact ? ' footer--compact' : ''} ${className}`.trim()}>
      <div className="footer__inner">
        {(logo || logoText) && (
          <div className="footer__brand">
            {logo || <span className="footer__logo-text">{logoText}</span>}
          </div>
        )}
        {links.length > 0 && (
          <nav className="footer__links" aria-label="푸터 링크">
            {links.map(link => (
              <a
                key={link.href}
                href={link.href}
                className="footer__link"
                {...(link.external ? { target: '_blank', rel: 'noopener noreferrer' } : {})}
              >
                {link.label}
                {link.external && <span className="footer__ext" aria-label="(새 창)">↗</span>}
              </a>
            ))}
          </nav>
        )}
        <div className="footer__right">
          {extra}
          {copyright && (
            <p className="footer__copyright">
              © {year} {copyright}
            </p>
          )}
        </div>
      </div>

      <style>{`
        .footer {
          border-top: 1px solid var(--color-border, #e2e8f0);
          background: var(--color-surface, #f8fafc);
          margin-top: auto;
        }
        .footer__inner {
          max-width: var(--max-w, 1280px); margin: 0 auto;
          padding: var(--space-lg, 24px) var(--space-lg, 24px);
          display: flex; align-items: center; justify-content: space-between;
          gap: 16px; flex-wrap: wrap;
        }
        .footer--compact .footer__inner { padding: var(--space-md, 16px) var(--space-lg, 24px); }
        .footer__brand { display: flex; align-items: center; }
        .footer__logo-text { font-size: 1rem; font-weight: 700; color: var(--color-primary, #3b82f6); }
        .footer__links { display: flex; flex-wrap: wrap; gap: 12px 20px; }
        .footer__link {
          font-size: 0.875rem; color: var(--color-text-muted, #64748b);
          text-decoration: none; display: flex; align-items: center; gap: 4px;
          transition: color 0.15s;
        }
        .footer__link:hover { color: var(--color-primary, #3b82f6); }
        .footer__link:focus-visible { outline: 2px solid var(--color-primary, #3b82f6); border-radius: 3px; }
        .footer__ext { font-size: 0.75rem; }
        .footer__right { display: flex; align-items: center; gap: 16px; }
        .footer__copyright { font-size: 0.8125rem; color: var(--color-text-muted, #64748b); margin: 0; }
        @media (max-width: 768px) {
          .footer__inner { flex-direction: column; align-items: flex-start; }
          .footer__right { width: 100%; }
        }
      `}</style>
    </footer>
  )
})
