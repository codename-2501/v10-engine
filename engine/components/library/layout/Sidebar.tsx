'use client'

import React, { useCallback } from 'react'

export interface SidebarItem {
  key: string
  label: string
  href: string
  icon?: React.ReactNode
  badge?: string | number
  children?: SidebarItem[]
}

export interface SidebarSection {
  title?: string
  items: SidebarItem[]
}

export interface SidebarProps {
  sections: SidebarSection[]
  activeKey?: string
  collapsed?: boolean
  onCollapse?: (collapsed: boolean) => void
  width?: number
  collapsedWidth?: number
  footer?: React.ReactNode
  className?: string
  overlay?: boolean
  onOverlayClick?: () => void
}

export const Sidebar = React.memo(function Sidebar({
  sections,
  activeKey,
  collapsed = false,
  onCollapse,
  width = 256,
  collapsedWidth = 64,
  footer,
  className = '',
  overlay = false,
  onOverlayClick,
}: SidebarProps) {
  const toggleCollapse = useCallback(() => onCollapse?.(!collapsed), [collapsed, onCollapse])

  return (
    <>
      {overlay && !collapsed && (
        <div
          className="sidebar-overlay"
          onClick={onOverlayClick}
          aria-hidden="true"
        />
      )}
      <aside
        className={`sidebar${collapsed ? ' sidebar--collapsed' : ''} ${className}`.trim()}
        style={{ width: collapsed ? collapsedWidth : width } as React.CSSProperties}
        aria-label="주요 메뉴"
      >
        <nav className="sidebar__nav">
          {sections.map((section, si) => (
            <div key={si} className="sidebar__section">
              {section.title && !collapsed && (
                <span className="sidebar__section-title">{section.title}</span>
              )}
              {section.items.map(item => (
                <SidebarNavItem
                  key={item.key}
                  item={item}
                  active={activeKey === item.key}
                  collapsed={collapsed}
                />
              ))}
            </div>
          ))}
        </nav>

        {footer && !collapsed && (
          <div className="sidebar__footer">{footer}</div>
        )}

        {onCollapse && (
          <button
            className="sidebar__collapse-btn"
            onClick={toggleCollapse}
            aria-label={collapsed ? '사이드바 펼치기' : '사이드바 접기'}
            type="button"
          >
            <svg
              width="16" height="16" viewBox="0 0 16 16" fill="none"
              style={{ transform: collapsed ? 'rotate(180deg)' : 'none', transition: 'transform 0.2s' }}
              aria-hidden="true"
            >
              <path d="M10 3L5 8l5 5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          </button>
        )}

        <style>{`
          .sidebar-overlay {
            position: fixed; inset: 0; z-index: 199;
            background: rgba(0, 0, 0, 0.4);
          }
          .sidebar {
            display: flex; flex-direction: column;
            height: 100%;
            background: var(--color-surface, #f8fafc);
            border-right: 1px solid var(--color-border, #e2e8f0);
            transition: width 0.22s cubic-bezier(0.4, 0, 0.2, 1);
            overflow: hidden;
            flex-shrink: 0;
            position: relative;
          }
          .sidebar__nav { flex: 1; overflow-y: auto; overflow-x: hidden; padding: var(--space-md, 16px) 0; }
          .sidebar__section { margin-bottom: var(--space-sm, 8px); }
          .sidebar__section-title {
            padding: 6px var(--space-md, 16px) 4px;
            font-size: 0.6875rem; font-weight: 700;
            color: var(--color-text-muted, #94a3b8);
            text-transform: uppercase; letter-spacing: 0.06em;
            white-space: nowrap;
          }
          .sidebar__footer {
            padding: var(--space-md, 16px);
            border-top: 1px solid var(--color-border, #e2e8f0);
          }
          .sidebar__collapse-btn {
            display: flex; align-items: center; justify-content: center;
            width: 24px; height: 24px;
            position: absolute; top: 50%; right: -12px;
            transform: translateY(-50%);
            background: var(--color-surface, #fff);
            border: 1px solid var(--color-border, #e2e8f0);
            border-radius: 50%;
            cursor: pointer;
            color: var(--color-text-muted, #64748b);
            z-index: 1;
            transition: background 0.15s;
          }
          .sidebar__collapse-btn:hover { background: var(--color-border, #e2e8f0); }
          .sidebar__collapse-btn:focus-visible { outline: 2px solid var(--color-primary, #3b82f6); }
          @media (prefers-reduced-motion: reduce) { .sidebar { transition: none; } }
        `}</style>
      </aside>
    </>
  )
})

interface SidebarNavItemProps {
  item: SidebarItem
  active: boolean
  collapsed: boolean
}

function SidebarNavItem({ item, active, collapsed }: SidebarNavItemProps) {
  return (
    <a
      href={item.href}
      className={`sidebar-item${active ? ' sidebar-item--active' : ''}`}
      aria-current={active ? 'page' : undefined}
      title={collapsed ? item.label : undefined}
    >
      {item.icon && <span className="sidebar-item__icon" aria-hidden="true">{item.icon}</span>}
      {!collapsed && (
        <>
          <span className="sidebar-item__label">{item.label}</span>
          {item.badge != null && (
            <span className="sidebar-item__badge" aria-label={`${item.badge}건`}>{item.badge}</span>
          )}
        </>
      )}

      <style>{`
        .sidebar-item {
          display: flex; align-items: center; gap: 10px;
          padding: 9px var(--space-md, 16px);
          font-size: 0.9375rem; font-weight: 500;
          color: var(--color-text-muted, #64748b);
          text-decoration: none;
          border-radius: 0;
          transition: background 0.12s, color 0.12s;
          white-space: nowrap; overflow: hidden;
          margin: 0 8px; border-radius: var(--radius-sm, 6px);
        }
        .sidebar-item:hover { background: var(--color-border, #e2e8f0); color: var(--color-text, #1a1a1a); }
        .sidebar-item--active {
          background: color-mix(in srgb, var(--color-primary, #3b82f6) 12%, transparent);
          color: var(--color-primary, #3b82f6);
          font-weight: 600;
        }
        .sidebar-item:focus-visible { outline: 2px solid var(--color-primary, #3b82f6); outline-offset: -2px; }
        .sidebar-item__icon { display: flex; align-items: center; flex-shrink: 0; font-size: 1.125rem; }
        .sidebar-item__label { flex: 1; overflow: hidden; text-overflow: ellipsis; }
        .sidebar-item__badge {
          min-width: 20px; height: 20px;
          padding: 0 6px;
          background: var(--color-primary, #3b82f6); color: #fff;
          font-size: 0.6875rem; font-weight: 700;
          border-radius: 999px;
          display: flex; align-items: center; justify-content: center;
          flex-shrink: 0;
        }
      `}</style>
    </a>
  )
}
