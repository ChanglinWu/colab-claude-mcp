function begin(logFile)
%CCMCP.BEGIN Prepare for a run from colab-claude-mcp.
%   Marks the figures that already exist, so that only figures made by the run count as new,
%   and starts a diary so that a long run's output can be read while it is still running.
    figs = findall(groot, 'Type', 'figure');
    for k = 1:numel(figs)
        setappdata(figs(k), 'ccmcp_seen', true);
    end
    diary('off');
    if nargin > 0 && ~isempty(logFile)
        diary(logFile);
    end
end
