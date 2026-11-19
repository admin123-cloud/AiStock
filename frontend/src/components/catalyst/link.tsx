import * as Headless from '@headlessui/react'
import React, { forwardRef } from 'react'
import { Link as RouterLink } from 'react-router-dom'

export const Link = forwardRef(function Link(
  props: { href: string } & Omit<React.ComponentPropsWithoutRef<'a'>, 'href'>,
  ref: React.ForwardedRef<HTMLAnchorElement>
) {
  return (
    <Headless.DataInteractive>
      <RouterLink {...props} to={props.href} ref={ref} />
    </Headless.DataInteractive>
  )
})
