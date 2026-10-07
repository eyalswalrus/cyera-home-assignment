import { createBrowserRouter } from 'react-router'
import { AppLayout } from './components/AppLayout'
import { RequireAuth, RequireGuest } from './components/RouteGuards'
import { DashboardPage } from './pages/DashboardPage'
import { LoginPage } from './pages/LoginPage'
import { RecentTicketsPage } from './pages/RecentTicketsPage'
import { RegisterPage } from './pages/RegisterPage'
import { SettingsPage } from './pages/SettingsPage'

export const routes = [
  {
    element: <RequireGuest />,
    children: [
      { path: '/login', element: <LoginPage /> },
      { path: '/register', element: <RegisterPage /> },
    ],
  },
  {
    element: <RequireAuth />,
    children: [
      {
        element: <AppLayout />,
        children: [
          { path: '/', element: <DashboardPage /> },
          { path: '/recent', element: <RecentTicketsPage /> },
          { path: '/settings', element: <SettingsPage /> },
        ],
      },
    ],
  },
]

export const router = createBrowserRouter(routes)
