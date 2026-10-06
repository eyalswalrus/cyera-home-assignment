import { Center, Loader } from '@mantine/core'
import { Navigate, Outlet, useLocation } from 'react-router'
import { useMe } from '../api/hooks'
import { ErrorState } from './ErrorState'

function FullPageLoader() {
  return (
    <Center h="100vh">
      <Loader />
    </Center>
  )
}

/** Pages behind login. Unauthenticated visitors go to /login and come back afterwards. */
export function RequireAuth() {
  const me = useMe()
  const location = useLocation()
  if (me.isPending) return <FullPageLoader />
  if (me.isError) return <ErrorState error={me.error} onRetry={() => me.refetch()} />
  if (!me.data) {
    const next = location.pathname + location.search
    return <Navigate to="/login" replace state={{ next }} />
  }
  return <Outlet />
}

/** Login/register: signed-in users go to the app - back to the page they originally asked for, if
 * any. This is the single place that redirects after login or registration. */
export function RequireGuest() {
  const me = useMe()
  const location = useLocation()
  if (me.isPending) return <FullPageLoader />
  if (me.data) {
    const next = (location.state as { next?: string } | null)?.next
    // Only same-app paths: never follow something like "//evil.example".
    const safeNext = next?.startsWith('/') && !next.startsWith('//') ? next : '/'
    return <Navigate to={safeNext} replace />
  }
  return <Outlet />
}
