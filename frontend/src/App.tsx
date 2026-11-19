import React, { Suspense, useEffect } from 'react'
import { Route, Routes, useLocation } from 'react-router-dom'
import { routes } from '@/router'
import { AppLayout } from '@/layouts/AppLayout'

const APP_TITLE = 'AiStock'

function RouteTitle() {
  const location = useLocation()
  useEffect(() => {
    const matched = routes.find(
      (route) =>
        route.path === location.pathname ||
        (route.path.includes(':') &&
          new RegExp(`^${route.path.replace(/:[^/]+/g, '[^/]+')}/?$`).test(location.pathname))
    )
    document.title = `${matched?.meta.title || 'AiStock'} - ${APP_TITLE}`
  }, [location])
  return null
}

function PageLoading() {
  return (
    <div className="flex h-64 items-center justify-center text-zinc-500 dark:text-zinc-400">
      页面加载中…
    </div>
  )
}

export default function App() {
  return (
    <AppLayout>
      <RouteTitle />
      <Suspense fallback={<PageLoading />}>
        <Routes>
          {routes.map((route) => (
            <Route key={route.path} path={route.path} element={<route.component />} />
          ))}
        </Routes>
      </Suspense>
    </AppLayout>
  )
}
