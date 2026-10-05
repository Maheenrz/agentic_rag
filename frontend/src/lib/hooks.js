import { useCallback, useEffect, useState } from 'react';

/** Loads data on mount (and when deps change). Returns { data, error, reload }. */
export function useAsync(fn, deps = []) {
  const [data, setData] = useState(null);
  const [error, setError] = useState('');

  const reload = useCallback(async () => {
    try {
      setError('');
      setData(await fn());
    } catch (e) {
      setError(e.message);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => {
    reload();
  }, [reload]);

  return { data, error, reload };
}

/** Wraps a button action so errors show as an alert instead of crashing the page. */
export function guard(fn) {
  return async () => {
    try {
      await fn();
    } catch (e) {
      alert(e.message);
    }
  };
}