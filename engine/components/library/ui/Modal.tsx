'use client'

import React, { useEffect, useRef, useCallback } from 'react'

export type ModalVariant = 'default' | 'confirm' | 'alert' | 'form'

export interface ModalProps {
  open: boolean
  onClose: () => void
  title?: string
  description?: string
  variant?: ModalVariant
  size?: 'sm' | 'md' | 'lg' | 'xl'
  children?: React.ReactNode
  footer?: React.ReactNode
  /** confirm 변형: 확인 버튼 텍스트 */
  confirmLabel?: string
  /** confirm 변형: 취소 버튼 텍스트 */
  cancelLabel?: string
  onConfirm?: () => void
  danger?: boolean
  hideClose?: boolean
}

export const Modal = React.memo(function Modal({
  open,
  onClose,
  title,
  description,
  variant = 'default',
  size = 'md',
  children,
  footer,
  confirmLabel = '확인',
  cancelLabel = '취소',
  onConfirm,
  danger = false,
  hideClose = false,
}: ModalProps) {
  const dialogRef = useRef<HTMLDivElement>(null)
  const prevActiveRef = useRef<Element | null>(null)

  useEffect(() => {
    if (open) {
      prevActiveRef.current = document.activeElement
      dialogRef.current?.focus()
      document.body.style.overflow = 'hidden'
    } else {
      document.body.style.overflow = ''
      if (prevActiveRef.current instanceof HTMLElement) {
        prevActiveRef.current.focus()
      }
    }
    return () => { document.body.style.overflow = '' }
  }, [open])

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'Escape' && open) onClose()
    if (e.key === 'Tab') {
      const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
      )
      if (!focusable || focusable.length === 0) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault()
        first.focus()
      }
    }
  }, [open, onClose])

  if (!open) return null

  return (
    <>
      <div
        className="modal-backdrop"
        onClick={onClose}
        aria-hidden="true"
      />
      <div
        ref={dialogRef}
        className={`modal modal--${size} modal--${variant}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={title ? 'modal-title' : undefined}
        aria-describedby={description ? 'modal-desc' : undefined}
        tabIndex={-1}
        onKeyDown={handleKeyDown}
      >
        <div className="modal__header">
          {title && <h2 id="modal-title" className="modal__title">{title}</h2>}
          {!hideClose && (
            <button
              className="modal__close"
              onClick={onClose}
              aria-label="닫기"
              type="button"
            >
              <svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true">
                <path d="M2 2l14 14M16 2L2 16" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/>
              </svg>
            </button>
          )}
        </div>
        {description && <p id="modal-desc" className="modal__desc">{description}</p>}
        {children && <div className="modal__body">{children}</div>}
        {(footer || variant === 'confirm') && (
          <div className="modal__footer">
            {footer || (
              <>
                <button
                  className="btn btn--secondary btn--md"
                  onClick={onClose}
                  type="button"
                >
                  {cancelLabel}
                </button>
                <button
                  className={`btn btn--${danger ? 'danger' : 'primary'} btn--md`}
                  onClick={onConfirm}
                  type="button"
                >
                  {confirmLabel}
                </button>
              </>
            )}
          </div>
        )}
      </div>

      <style>{`
        .modal-backdrop {
          position: fixed; inset: 0;
          background: rgba(0, 0, 0, 0.5);
          backdrop-filter: blur(2px);
          z-index: 1000;
          animation: modal-fade-in 0.15s ease;
        }
        .modal {
          position: fixed;
          top: 50%; left: 50%;
          transform: translate(-50%, -50%);
          background: var(--color-surface, #fff);
          border: 1px solid var(--color-border, #e2e8f0);
          border-radius: var(--radius-lg, 16px);
          box-shadow: var(--shadow-xl, 0 20px 60px rgba(0,0,0,0.18));
          z-index: 1001;
          max-height: 90vh;
          overflow-y: auto;
          outline: none;
          animation: modal-slide-in 0.18s ease;
          width: 90vw;
        }
        .modal--sm { max-width: 400px; }
        .modal--md { max-width: 560px; }
        .modal--lg { max-width: 760px; }
        .modal--xl { max-width: 1000px; }
        .modal__header {
          display: flex; align-items: center; justify-content: space-between;
          padding: 20px 24px 0;
          gap: 12px;
        }
        .modal__title {
          font-size: 1.125rem;
          font-weight: 600;
          color: var(--color-text, #1a1a1a);
          margin: 0;
        }
        .modal__close {
          display: flex; align-items: center; justify-content: center;
          width: 32px; height: 32px;
          background: transparent;
          border: none;
          border-radius: var(--radius-sm, 4px);
          cursor: pointer;
          color: var(--color-text-muted, #64748b);
          flex-shrink: 0;
          transition: background 0.15s;
        }
        .modal__close:hover { background: var(--color-border, #e2e8f0); }
        .modal__close:focus-visible {
          outline: 2px solid var(--color-primary, #3b82f6);
          outline-offset: 2px;
        }
        .modal__desc {
          padding: 8px 24px 0;
          font-size: 0.9375rem;
          color: var(--color-text-muted, #64748b);
          margin: 0;
        }
        .modal__body { padding: 16px 24px; }
        .modal__footer {
          display: flex; justify-content: flex-end; gap: 10px;
          padding: 0 24px 20px;
        }
        @keyframes modal-fade-in { from { opacity: 0 } to { opacity: 1 } }
        @keyframes modal-slide-in {
          from { opacity: 0; transform: translate(-50%, -48%); }
          to { opacity: 1; transform: translate(-50%, -50%); }
        }
        @media (prefers-reduced-motion: reduce) {
          .modal-backdrop, .modal { animation: none; }
        }
        @media (max-width: 480px) {
          .modal {
            top: auto; left: 0; right: 0; bottom: 0;
            transform: none;
            width: 100%;
            max-width: 100%;
            border-bottom-left-radius: 0;
            border-bottom-right-radius: 0;
            max-height: 85vh;
          }
          @keyframes modal-slide-in {
            from { transform: translateY(100%); }
            to { transform: translateY(0); }
          }
        }
      `}</style>
    </>
  )
})
