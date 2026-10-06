import { zodResolver } from '@hookform/resolvers/zod'
import { Alert, Anchor, Button, PasswordInput, Stack, Text, TextInput } from '@mantine/core'
import { useForm } from 'react-hook-form'
import { Link, useLocation } from 'react-router'
import { z } from 'zod'
import { useLogin } from '../api/hooks'
import { AuthCard } from '../components/AuthCard'

const schema = z.object({
  email: z.email('Enter a valid email address.'),
  password: z.string().min(1, 'Enter your password.'),
})
type Values = z.infer<typeof schema>

export function LoginPage() {
  const login = useLogin()
  const location = useLocation()
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: { email: '', password: '' } })

  // On success the session query refreshes and RequireGuest redirects into the app.
  const onSubmit = form.handleSubmit((values) => login.mutate(values))

  return (
    <AuthCard title="Sign in" subtitle="Report non-human identity findings to Jira.">
      <form onSubmit={onSubmit} noValidate>
        <Stack>
          {login.error && <Alert color="red">{login.error.message}</Alert>}
          <TextInput
            label="Email"
            type="email"
            autoComplete="email"
            autoFocus
            {...form.register('email')}
            error={form.formState.errors.email?.message}
          />
          <PasswordInput
            label="Password"
            autoComplete="current-password"
            {...form.register('password')}
            error={form.formState.errors.password?.message}
          />
          <Button type="submit" loading={login.isPending} fullWidth>
            Sign in
          </Button>
          <Text size="sm" ta="center">
            No account yet?{' '}
            <Anchor component={Link} to="/register" state={location.state}>
              Create one
            </Anchor>
          </Text>
        </Stack>
      </form>
    </AuthCard>
  )
}
