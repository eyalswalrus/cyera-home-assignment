import { zodResolver } from '@hookform/resolvers/zod'
import { Alert, Anchor, Button, PasswordInput, Stack, Text, TextInput } from '@mantine/core'
import { useForm } from 'react-hook-form'
import { Link } from 'react-router'
import { z } from 'zod'
import { ApiError } from '../api/client'
import { useLogin, useRegister } from '../api/hooks'
import { AuthCard } from '../components/AuthCard'

const MIN_PASSWORD = 12 // mirrors the server's policy; the server stays the authority

const schema = z
  .object({
    email: z.email('Enter a valid email address.'),
    password: z.string().min(MIN_PASSWORD, `Use at least ${MIN_PASSWORD} characters.`),
    confirm: z.string(),
  })
  .refine((v) => v.password === v.confirm, { path: ['confirm'], message: "Passwords don't match." })
type Values = z.infer<typeof schema>

export function RegisterPage() {
  const register = useRegister()
  const login = useLogin()
  const form = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: { email: '', password: '', confirm: '' },
  })

  const onSubmit = form.handleSubmit(async ({ email, password }) => {
    try {
      await register.mutateAsync({ email, password })
      // Straight into the app, no second form: RequireGuest redirects once the session exists.
      await login.mutateAsync({ email, password })
    } catch (error) {
      if (error instanceof ApiError && error.code === 'REGISTER_INVALID_PASSWORD') {
        form.setError('password', { message: error.message })
      } else if (error instanceof ApiError && error.fieldErrors.email) {
        form.setError('email', { message: 'Enter a valid email address.' })
      }
    }
  })

  const error = register.error ?? login.error
  const showBanner = error && !(error instanceof ApiError && error.code === 'REGISTER_INVALID_PASSWORD')

  return (
    <AuthCard title="Create your account" subtitle="You'll connect your Jira account next.">
      <form onSubmit={onSubmit} noValidate>
        <Stack>
          {showBanner && <Alert color="red">{error.message}</Alert>}
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
            description={`At least ${MIN_PASSWORD} characters, not containing your email.`}
            autoComplete="new-password"
            {...form.register('password')}
            error={form.formState.errors.password?.message}
          />
          <PasswordInput
            label="Confirm password"
            autoComplete="new-password"
            {...form.register('confirm')}
            error={form.formState.errors.confirm?.message}
          />
          <Button type="submit" loading={register.isPending || login.isPending} fullWidth>
            Create account
          </Button>
          <Text size="sm" ta="center">
            Already have an account?{' '}
            <Anchor component={Link} to="/login">
              Sign in
            </Anchor>
          </Text>
        </Stack>
      </form>
    </AuthCard>
  )
}
