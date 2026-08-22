import defaultMdxComponents from 'fumadocs-ui/mdx';
import { Step, Steps } from 'fumadocs-ui/components/steps';
import type { MDXComponents } from 'mdx/types';
import * as Python from 'fumadocs-python/components';
import { PySourceCode } from './python-source';

export function getMDXComponents(components?: MDXComponents) {
  return {
    ...defaultMdxComponents,
    Step,
    Steps,
    // PyFunction, PyParameter, PyAttribute and friends used by the generated
    // developer reference. PySourceCode is swapped for a client-side version;
    // see python-source.tsx.
    ...Python,
    PySourceCode,
    ...components,
  } satisfies MDXComponents;
}

export const useMDXComponents = getMDXComponents;

declare global {
  type MDXProvidedComponents = ReturnType<typeof getMDXComponents>;
}
