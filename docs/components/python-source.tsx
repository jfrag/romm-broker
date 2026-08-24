'use client';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from 'fumadocs-ui/components/ui/collapsible';
import { buttonVariants } from 'fumadocs-ui/components/ui/button';
import { ChevronRight } from 'lucide-react';
import type { ReactNode } from 'react';
import { cn } from '@/lib/cn';

// Drop-in replacement for fumadocs-python's PySourceCode. The upstream one is
// a server component that hands a function-valued className to a client
// collapsible, which the static export refuses to serialise; keeping the whole
// widget on the client side avoids that.
export function PySourceCode({ children }: { children: ReactNode }) {
  return (
    <Collapsible className="my-6">
      <CollapsibleTrigger className={cn(buttonVariants({ color: 'secondary', size: 'sm', className: 'group' }))}>
        Source Code
        <ChevronRight className="size-3.5 text-fd-muted-foreground group-data-[panel-open]:rotate-90" />
      </CollapsibleTrigger>
      <CollapsibleContent className="prose-no-margin">{children}</CollapsibleContent>
    </Collapsible>
  );
}
