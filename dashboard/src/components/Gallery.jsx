import React, { useState, useEffect, useRef, useCallback } from 'react';
import { Loader2 } from 'lucide-react';
import { getApiUrl } from '../config';
import GalleryCard from './GalleryCard';
import Skeleton from './ui/Skeleton';

const CLIPS_PER_PAGE = 20;

export default function Gallery() {
    const [clips, setClips] = useState([]);
    const [loading, setLoading] = useState(true);
    const [loadingMore, setLoadingMore] = useState(false);
    const [error, setError] = useState(null);
    const [hasMore, setHasMore] = useState(true);
    const [offset, setOffset] = useState(0);

    const loaderRef = useRef(null);

    const fetchClips = useCallback(async (currentOffset = 0, append = false) => {
        try {
            if (currentOffset === 0) setLoading(true);
            else setLoadingMore(true);

            const res = await fetch(
                getApiUrl(`/api/gallery/clips?limit=${CLIPS_PER_PAGE}&offset=${currentOffset}`)
            );
            if (!res.ok) throw new Error('Failed to fetch clips');
            const data = await res.json();

            const newClips = data.clips || [];

            if (append) {
                setClips(prev => [...prev, ...newClips]);
            } else {
                setClips(newClips);
            }

            setHasMore(data.has_more ?? newClips.length === CLIPS_PER_PAGE);
            setOffset(currentOffset + newClips.length);
        } catch (err) {
            setError(err.message);
        } finally {
            setLoading(false);
            setLoadingMore(false);
        }
    }, []);

    // Initial load
    useEffect(() => {
        fetchClips(0, false);
    }, [fetchClips]);

    // Infinite scroll observer
    useEffect(() => {
        if (!hasMore || loadingMore || loading) return;

        const observer = new IntersectionObserver(
            (entries) => {
                if (entries[0].isIntersecting && hasMore && !loadingMore) {
                    fetchClips(offset, true);
                }
            },
            { rootMargin: '200px', threshold: 0.1 }
        );

        const node = loaderRef.current;
        if (node) {
            observer.observe(node);
        }

        return () => {
            if (node) {
                observer.unobserve(node);
            }
        };
    }, [hasMore, loadingMore, loading, offset, fetchClips]);

    if (loading) {
        return (
            <div className="h-full overflow-y-auto p-6 md:p-8" aria-busy="true" aria-label="Loading clips">
                <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4 gap-6">
                    {[0, 1, 2, 3].map((n) => (
                        <div key={n} className="card overflow-hidden">
                            <Skeleton className="aspect-[9/16] !rounded-none" />
                            <div className="p-4 space-y-2"><Skeleton className="h-4 w-3/4" /><Skeleton className="h-3 w-1/3" /></div>
                        </div>
                    ))}
                </div>
            </div>
        );
    }

    if (error) {
        return (
            <div role="alert" className="h-full flex flex-col items-center justify-center text-center p-6">
                <h2 className="font-display text-3xl text-ink mb-2">The gallery did not load.</h2>
                <p className="text-sm text-muted">{error}</p>
                <button
                    onClick={() => {
                        setError(null);
                        setOffset(0);
                        fetchClips(0, false);
                    }}
                    className="btn-quiet mt-4"
                >
                    Retry
                </button>
            </div>
        );
    }

    return (
        <div className="h-full overflow-y-auto p-6 md:p-8 animate-fade">
            <div className="flex items-end justify-between mb-8">
                <div>
                    <p className="eyebrow mb-1.5">Library</p>
                    <h1 className="font-display lowercase text-2xl text-ink">Clip Gallery</h1>
                </div>
                <span className="readout">
                    {clips.length} {clips.length === 1 ? 'Clip' : 'Clips'}{hasMore ? '+' : ''}
                </span>
            </div>

            {clips.length === 0 ? (
                <div className="py-20 max-w-md">
                    <h2 className="font-display text-4xl text-ink mb-2">No clips yet</h2>
                    <p className="text-sm text-muted">Clips from your finished jobs collect here. Make the first one from the Clip Generator.</p>
                </div>
            ) : (
                <>
                    <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4 gap-6 pb-10">
                        {clips.map((clip) => (
                            <GalleryCard key={`${clip.job_id}-${clip.index}`} clip={clip} />
                        ))}
                    </div>

                    {/* Infinite scroll loader trigger */}
                    {hasMore && (
                        <div
                            ref={loaderRef}
                            className="flex justify-center py-8"
                        >
                            {loadingMore && (
                                <div className="flex items-center gap-2 text-muted">
                                    <Loader2 size={20} className="animate-spin" />
                                    <span className="text-sm lowercase">Loading more clips...</span>
                                </div>
                            )}
                        </div>
                    )}
                </>
            )}
        </div>
    );
}
