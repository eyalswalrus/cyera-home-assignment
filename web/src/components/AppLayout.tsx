import { AppShell, Button, Container, Group, Menu, Text, UnstyledButton } from '@mantine/core'
import { notifications } from '@mantine/notifications'
import { IconChevronDown, IconLogout, IconSettings, IconShieldLock } from '@tabler/icons-react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router'
import { useLogout, useMe } from '../api/hooks'

const NAV = [
  { to: '/', label: 'Report finding' },
  { to: '/recent', label: 'Recent tickets' },
]

export function AppLayout() {
  const me = useMe()
  const logout = useLogout()
  const navigate = useNavigate()
  const { pathname } = useLocation()

  const signOut = () =>
    logout.mutate(undefined, {
      onSuccess: () => navigate('/login', { replace: true }),
      onError: (error) => notifications.show({ color: 'red', title: "Couldn't sign out", message: error.message }),
    })

  return (
    <AppShell header={{ height: 60 }} padding="md">
      <AppShell.Header>
        <Container size="xl" h="100%">
          <Group h="100%" justify="space-between" wrap="nowrap">
            <Group gap="lg" wrap="nowrap">
              <Group gap={6} wrap="nowrap">
                <IconShieldLock size={22} color="var(--mantine-primary-color-filled)" />
                <Text fw={700}>IdentityHub</Text>
              </Group>
              <Group gap={4} visibleFrom="xs">
                {NAV.map((item) => (
                  <Button
                    key={item.to}
                    component={NavLink}
                    to={item.to}
                    variant={pathname === item.to ? 'light' : 'subtle'}
                    size="compact-md"
                  >
                    {item.label}
                  </Button>
                ))}
              </Group>
            </Group>

            <Menu position="bottom-end" width={220}>
              <Menu.Target>
                <UnstyledButton aria-label="Account menu">
                  <Group gap={4} wrap="nowrap">
                    <Text size="sm" truncate maw={180}>
                      {me.data?.email}
                    </Text>
                    <IconChevronDown size={14} />
                  </Group>
                </UnstyledButton>
              </Menu.Target>
              <Menu.Dropdown>
                {NAV.map((item) => (
                  <Menu.Item key={item.to} hiddenFrom="xs" component={NavLink} to={item.to}>
                    {item.label}
                  </Menu.Item>
                ))}
                <Menu.Divider hiddenFrom="xs" />
                <Menu.Item leftSection={<IconSettings size={16} />} component={NavLink} to="/settings">
                  Settings
                </Menu.Item>
                <Menu.Divider />
                <Menu.Item leftSection={<IconLogout size={16} />} onClick={signOut} disabled={logout.isPending}>
                  Sign out
                </Menu.Item>
              </Menu.Dropdown>
            </Menu>
          </Group>
        </Container>
      </AppShell.Header>

      <AppShell.Main>
        <Container size="xl" py="md">
          <Outlet />
        </Container>
      </AppShell.Main>
    </AppShell>
  )
}
