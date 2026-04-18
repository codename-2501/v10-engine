'use client'

import { useCallback } from 'react'
import { useAuthStore } from '../stores/authStore'

export function useAuth() {
  const { user, token, isLoading, isAuthenticated, setAuth, clearAuth, setLoading } = useAuthStore()

  const login = useCallback(async (credentials: { email: string; password: string }) => {
    setLoading(true)
    try {
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(credentials),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.message || '로그인에 실패했습니다.')
      }
      const { user, token } = await res.json()
      setAuth(user, token)
      return { user, token }
    } finally {
      setLoading(false)
    }
  }, [setAuth, setLoading])

  const logout = useCallback(async () => {
    setLoading(true)
    try {
      await fetch('/api/auth/logout', { method: 'POST' }).catch(() => {})
    } finally {
      clearAuth()
      setLoading(false)
    }
  }, [clearAuth, setLoading])

  const refreshToken = useCallback(async () => {
    try {
      const res = await fetch('/api/auth/refresh', { method: 'POST' })
      if (res.ok) {
        const { token: newToken } = await res.json()
        if (user) setAuth(user, newToken)
      } else {
        clearAuth()
      }
    } catch {
      clearAuth()
    }
  }, [user, setAuth, clearAuth])

  return {
    user,
    token,
    isLoading,
    isAuthenticated,
    login,
    logout,
    refreshToken,
  }
}
