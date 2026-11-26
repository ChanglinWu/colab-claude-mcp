function p = pool(kind, workers)
%CCMCP.POOL Start or reuse a parallel pool for parfor / parfeval.
%   ccmcp.pool                   reuse the open process pool, or start one with the cluster's
%                                default number of workers (all cores); same as 'processes'
%   ccmcp.pool('processes', n)   a process pool with n workers (15-20 s to start; works with
%                                every toolbox, PDE Toolbox geometry and meshing included)
%   ccmcp.pool('threads')        a thread pool (starts in about a second, low memory, but many
%                                toolbox functions refuse thread workers)
%   ccmcp.pool('off')            shut the open pool down
%   An open pool of the requested kind is reused, whatever its size, unless n is given and
%   differs; a pool of the other kind is shut down first. Pools close by themselves after 30
%   idle minutes (the profile's IdleTimeout).
    if nargin < 1 || isempty(kind)
        kind = 'processes';
    end
    if nargin < 2
        workers = [];
    end
    kind = lower(char(kind));
    if ~ismember(kind, {'processes', 'threads', 'off'})
        error('ccmcp:pool', 'Unknown pool kind ''%s''; use processes, threads or off.', kind);
    end
    if ~(license('test', 'Distrib_Computing_Toolbox') && ~isempty(ver('parallel')))
        error('ccmcp:pool', 'The Parallel Computing Toolbox is not available; run the loop serially.');
    end
    current = gcp('nocreate');
    if strcmp(kind, 'off')
        if ~isempty(current)
            delete(current);
        end
        if nargout > 0
            p = [];
        end
        return
    end
    if ~isempty(current)
        isThreads = isa(current, 'parallel.ThreadPool');
        sameKind = strcmp(kind, 'threads') == isThreads;
        if sameKind && (isempty(workers) || current.NumWorkers == workers)
            if nargout > 0
                p = current;
            end
            return
        end
        delete(current);
    end
    if strcmp(kind, 'threads')
        profile = 'Threads';
    else
        profile = 'Processes';
    end
    if isempty(workers)
        started = parpool(profile);
    else
        started = parpool(profile, workers);
    end
    if nargout > 0
        p = started;
    end
end
