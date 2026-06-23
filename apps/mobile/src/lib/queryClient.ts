import { defaultQueryOptions } from "@kinora/core";
import { QueryClient } from "@tanstack/react-query";

export const queryClient = new QueryClient({
  defaultOptions: { queries: defaultQueryOptions },
});
