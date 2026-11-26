function info = compute()
%CCMCP.COMPUTE Compute resources this MATLAB session can use for parallel or accelerated work.
%   ccmcp.compute        prints a short summary.
%   info = ccmcp.compute returns a struct with fields
%     cores       physical cores MATLAB sees (built-in functions already multithread on them)
%     threads     maxNumCompThreads
%     pct         true when the Parallel Computing Toolbox is installed and licensed
%     maxWorkers  workers a 'Processes' pool can have (0 without the toolbox)
%     pool        open pool: '' (none), 'Threads' or 'Processes'
%     poolWorkers workers of the open pool (0 without one)
%     gpu         true when MATLAB can compute on a GPU (gpuArray); MATLAB supports NVIDIA
%                 GPUs only, so this is false on Apple-silicon Macs (no Metal/MPS support)
%     gpuName     name of the selected GPU, or ''
%   It does not start a pool or select a GPU. ccmcp.pool starts or reuses a pool.
    s.cores = feature('numcores');
    s.threads = maxNumCompThreads;
    s.pct = license('test', 'Distrib_Computing_Toolbox') && ~isempty(ver('parallel'));
    s.maxWorkers = 0;
    s.pool = '';
    s.poolWorkers = 0;
    s.gpu = false;
    s.gpuName = '';
    if s.pct
        try
            c = parcluster('Processes');
            s.maxWorkers = c.NumWorkers;
        catch
        end
        p = gcp('nocreate');
        if ~isempty(p)
            s.poolWorkers = p.NumWorkers;
            if isa(p, 'parallel.ThreadPool')
                s.pool = 'Threads';
            else
                s.pool = 'Processes';
            end
        end
        try
            s.gpu = canUseGPU;
            if s.gpu
                s.gpuName = gpuDevice().Name;
            end
        catch
            s.gpu = false;
        end
    end
    if nargout > 0
        info = s;
        return
    end
    fprintf('Compute: %d cores, %d computational threads (built-in functions multithread on them)\n', ...
        s.cores, s.threads);
    if ~s.pct
        fprintf('Parallel Computing Toolbox: not available, so no parfor pool or GPU\n');
        return
    end
    if isempty(s.pool)
        fprintf('Parallel pool: none open; ccmcp.pool starts one (Processes, up to %d workers)\n', s.maxWorkers);
    else
        fprintf('Parallel pool: %s, %d workers (open)\n', s.pool, s.poolWorkers);
    end
    if s.gpu
        fprintf('GPU: %s (gpuArray available)\n', s.gpuName);
    else
        fprintf('GPU: none usable by MATLAB (MATLAB computes on NVIDIA GPUs only)\n');
    end
end
