function finish(outDir, prefix, which, closeAfter, resolution)
%CCMCP.FINISH End a run from colab-claude-mcp: stop the diary and export figures to PNG.
%   which is 'new' (figures made by the run), 'all' or 'none'. The PNG paths are written,
%   one per line, to <outDir>/<prefix>.figs so that the MCP server can pick them up.
    diary('off');
    if nargin < 5 || isempty(resolution)
        resolution = 110;
    end
    files = {};
    figs = findall(groot, 'Type', 'figure');
    if ~isempty(figs) && ~strcmp(which, 'none')
        numbers = zeros(1, numel(figs));
        for k = 1:numel(figs)
            n = figs(k).Number;
            if isempty(n), n = inf; end
            numbers(k) = n;
        end
        [~, order] = sort(numbers);
        figs = figs(order);
        for k = 1:numel(figs)
            h = figs(k);
            if strcmp(which, 'new') && isappdata(h, 'ccmcp_seen')
                continue
            end
            file = fullfile(outDir, sprintf('%s_fig%d.png', prefix, numel(files) + 1));
            try
                exportgraphics(h, file, 'Resolution', resolution);
            catch
                try
                    print(h, file, '-dpng', sprintf('-r%d', resolution));
                catch err
                    warning('ccmcp:export', 'Could not export figure: %s', err.message);
                    continue
                end
            end
            files{end + 1} = file; %#ok<AGROW>
        end
    end
    for k = 1:numel(figs)
        if ~isvalid(figs(k)), continue, end
        if closeAfter && (strcmp(which, 'all') || ~isappdata(figs(k), 'ccmcp_seen'))
            close(figs(k));
        else
            setappdata(figs(k), 'ccmcp_seen', true);
        end
    end
    fid = fopen(fullfile(outDir, [prefix '.figs']), 'w');
    if fid > 0
        fprintf(fid, '%s\n', files{:});
        fclose(fid);
    end
end
