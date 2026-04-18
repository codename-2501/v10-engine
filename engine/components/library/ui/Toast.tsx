'use client'

import React, { useEffect, useCallback } from 'react'

export type ToastType = 'success' | 'error' | 'warning' | 'info'

export interface ToastItem {
  id: string
  type: ToastType
  title: string
  message?: string
  duration?: number
}

interface ToastProps {
  item: ToastItem
  onDismiss: (id: string) => void
}

const ICONS: Record<ToastType, React.ReactNode> = {
  success: (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true">
      <circle cx="9" cy="9" r="8" stroke="currentColor" strokeWidth="1.5"/>
      <path d="M5 9l3 3 5-5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  ),
  error: (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true">
      <circle cx="9" cy="9" r="8" stroke="currentColor" strokeWidth="1.5"/>
      <path d="M6 6l6 6M12 6l-6 6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
    </svg>
  ),
  warning: (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true">
      <path d="M9 2L1 16h16L9 2z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round"/>
      <path d="M9 8v4M9 13.5v.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
    </svg>
  ),
  info: (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true">
      <circle cx="9" cy="9" r="8" stroke="currentColor" strokeWidth="1.5"/>
      <path d="M9 8v5M9 5.5v.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
    </svg>
  ),
}

const Toast = React.memo(function Toast({ item, onDismiss }: ToastProps) {
  const dismiss = useCallback(() => onDismiss(item.id), [item.id, onDismiss])

  useEffect(() => {
    const duration = item.duration ?? 4500
    if (duration > 0) {
      const timer = setTimeout(dismiss, duration)
      return () => clearTimeout(timer)
    }
  }, [dismiss, item.duration])

  return (
    <div
      className={`toast toast--${item.type}`}
      role={item.type === 'error' ? 'alert' : 'status'}
      aria-live={item.type === 'error' ? 'assertive' : 'polite'}
      aria-atomic="true"
    >
      <span className="toast__icon">{ICONS[item.type]}</span>
      <div className="toast__content">
        <span className="toast__title">{item.title}</span>
        {item.message && <span className="toast__message">{item.message}</span>}
      </div>
      <button
        className="toast__close"
        onClick={dismiss}
        aria-label="알림 닫기"
        type="button"
      >
        <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
          <path d="M1 1l12 12M13 1L1 13" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
        </svg>
      </button>
    </div>
  )
})

export interface ToastContainerProps {
  items: ToastItem[]
  onDismiss: (id: string) => void
  position?: 'top-right' | 'top-left' | 'bottom-right' | 'bottom-left' | 'top-center'
}

export const ToastContainer = React.memo(function ToastContainer({
  items,
  onDismiss,
  position = 'top-right',
}: ToastContainerProps) {
  const posClass = `toast-container--${position}`

  return (
    <div
      className={`toast-container ${posClass}`}
      aria-label="알림"
      role="region"
    >
      {items.map(item => (
        <Toast key={item.id} item={item} onDismiss={onDismiss} />
      ))}

      <style>{`
        .toast-container {
          position: fixed;
          z-index: 2000;
          display: flex;
          flex-direction: column;
          gap: 10px;
          max-width: 380px;
          width: calc(100vw - 32px);
          pointer-events: none;
        }
        .toast-container--top-right { top: 20px; right: 16px; }
        .toast-container--top-left { top: 20px; left: 16px; }
        .toast-container--top-center { top: 20px; left: 50%; transform: translateX(-50%); }
        .toast-container--bottom-right { bottom: 20px; right: 16px; }
        .toast-container--bottom-left { bottom: 20px; left: 16px; }
        .toast {
          display: flex;
          align-items: flex-start;
          gap: 12px;
          padding: 14px 16px;
          background: var(--color-surface, #fff);
          border: 1px solid var(--color-border, #e2e8f0);
          border-radius: var(--radius-md, 8px);
          box-shadow: var(--shadow-md, 0 4px 12px rgba(0,0,0,0.12));
          pointer-events: all;
          animation: toast-in 0.2s ease;
          border-left-width: 4px;
        }
        .toast--success { border-left-color: var(--color-success, #22c55e); }
        .toast--success .toast__icon { color: var(--color-success, #22c55e); }
        .toast--error { border-left-color: var(--color-danger, #ef4444); }
        .toast--error .toast__icon { color: var(--color-danger, #ef4444); }
        .toast--warning { border-left-color: var(--color-warning, #f59e0b); }
        .toast--warning .toast__icon { color: var(--color-warning, #f59e0b); }
        .toast--info { border-left-color: var(--color-primary, #3b82f6); }
        .toast--info .toast__icon { color: var(--color-primary, #3b82f6); }
        .toast__icon { display: flex; align-items: center; flex-shrink: 0; margin-top: 1px; }
        .toast__content { flex: 1; display: flex; flex-direction: column; gap: 3px; min-width: 0; }
        .toast__title { font-size: 0.9375rem; font-weight: 600; color: var(--color-text, #1a1a1a); }
        .toast__message { font-size: 0.8125rem; color: var(--color-text-muted, #64748b); }
        .toast__close {
          display: flex; align-items: center; justify-content: center;
          width: 24px; height: 24px;
          background: transparent; border: none; border-radius: 4px;
          cursor: pointer; color: var(--color-text-muted, #64748b);
          flex-shrink: 0;
          transition: background 0.15s;
        }
        .toast__close:hover { background: var(--color-border, #e2e8f0); }
        .toast__close:focus-visible { outline: 2px solid var(--color-primary, #3b82f6); outline-offset: 2px; }
        @keyframes toast-in { from { opacity: 0; transform: translateX(20px); } to { opacity: 1; transform: none; } }
        @media (prefers-reduced-motion: reduce) { .toast { animation: none; } }
      `}</style>
    </div>
  )
})
