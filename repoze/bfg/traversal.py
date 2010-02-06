import urllib

from zope.interface import implements
from zope.interface.interfaces import IInterface

from repoze.lru import lru_cache

from repoze.bfg.interfaces import IContextURL
from repoze.bfg.interfaces import ITraverser
from repoze.bfg.interfaces import VH_ROOT_KEY

from repoze.bfg.encode import url_quote
from repoze.bfg.location import lineage
from repoze.bfg.request import Request
from repoze.bfg.threadlocal import get_current_registry

def find_root(model):
    """ Find the root node in the graph to which ``model``
    belongs. Note that ``model`` should be :term:`location`-aware.
    Note that the root node is available in the request object by
    accessing the ``request.root`` attribute.
    """
    for location in lineage(model):
        if location.__parent__ is None:
            model = location
            break
    return model

def find_model(model, path):
    """ Given a model object and a string or tuple representing a path
    (such as the return value of
    :func:`repoze.bfg.traversal.model_path` or
    :func:`repoze.bfg.traversal.model_path_tuple`), return a context
    in this application's model graph at the specified path.  The
    model passed in *must* be :term:`location`-aware.  If the path
    cannot be resolved (if the respective node in the graph does not
    exist), a :exc:`KeyError` will be raised.

    This function is the logical inverse of
    :func:`repoze.bfg.traversal.model_path` and
    :func:`repoze.bfg.traversal.model_path_tuple`; it can resolve any
    path string or tuple generated by either of those functions.

    Rules for passing a *string* as the ``path`` argument: if the
    first character in the path string is the with the ``/``
    character, the path will considered absolute and the graph
    traversal will start at the root object.  If the first character
    of the path string is *not* the ``/`` character, the path is
    considered relative and graph traversal will begin at the model
    object supplied to the function as the ``model`` argument.  If an
    empty string is passed as ``path``, the ``model`` passed in will
    be returned.  Model path strings must be escaped in the following
    manner: each Unicode path segment must be encoded as UTF-8 and as
    each path segment must escaped via Python's :mod:`urllib.quote`.
    For example, ``/path/to%20the/La%20Pe%C3%B1a`` (absolute) or
    ``to%20the/La%20Pe%C3%B1a`` (relative).  The
    :func:`repoze.bfg.traversal.model_path` function generates strings
    which follow these rules (albeit only absolute ones).

    Rules for passing a *tuple* as the ``path`` argument: if the first
    element in the path tuple is the empty string (for example ``('',
    'a', 'b', 'c')``, the path is considered absolute and the graph
    traversal will start at the graph root object.  If the first
    element in the path tuple is not the empty string (for example
    ``('a', 'b', 'c')``), the path is considered relative and graph
    traversal will begin at the model object supplied to the function
    as the ``model`` argument.  If an empty sequence is passed as
    ``path``, the ``model`` passed in itself will be returned.  No
    URL-quoting or UTF-8-encoding of individual path segments within
    the tuple is required (each segment may be any string or unicode
    object representing a model name).  Model path tuples generated by
    :func:`repoze.bfg.traversal.model_path_tuple` can always be
    resolved by ``find_model``.
    """
    D = traverse(model, path)
    view_name = D['view_name']
    context = D['context']
    if view_name:
        raise KeyError('%r has no subelement %s' % (context, view_name))
    return context

def find_interface(model, class_or_interface):
    """
    Return the first object found in the parent chain of ``model``
    which, a) if ``class_or_interface`` is a Python class object, is
    an instance of the class or any subclass of that class or b) if
    ``class_or_interface`` is a :term:`interface`, provides the
    specified interface.  Return ``None`` if no object providing
    ``interface_or_class`` can be found in the parent chain.  The
    ``model`` passed in *must* be :term:`location`-aware.
    """
    if IInterface.providedBy(class_or_interface):
        test = class_or_interface.providedBy
    else:
        test = lambda arg: isinstance(arg, class_or_interface)
    for location in lineage(model):
        if test(location):
            return location

def model_path(model, *elements):
    """ Return a string object representing the absolute physical path
    of the model object based on its position in the model graph, e.g
    ``/foo/bar``.  Any positional arguments passed in as ``elements``
    will be appended as path segments to the end of the model path.
    For instance, if the model's path is ``/foo/bar`` and ``elements``
    equals ``('a', 'b')``, the returned string will be
    ``/foo/bar/a/b``.  The first character in the string will always
    be the ``/`` character (a leading ``/`` character in a path string
    represents that the path is absolute).

    Model path strings returned will be escaped in the following
    manner: each unicode path segment will be encoded as UTF-8 and
    each path segment will be escaped via Python's :mod:`urllib.quote`.
    For example, ``/path/to%20the/La%20Pe%C3%B1a``.

    This function is a logical inverse of
    :mod:`repoze.bfg.traversal.find_model`: it can be used to generate
    path references that can later be resolved via that function.

    The ``model`` passed in *must* be :term:`location`-aware.

    .. note:: Each segment in the path string returned will use the
              ``__name__`` attribute of the model it represents within
              the graph.  Each of these segments *should* be a unicode
              or string object (as per the contract of
              :term:`location`-awareness).  However, no conversion or
              safety checking of model names is performed.  For
              instance, if one of the models in your graph has a
              ``__name__`` which (by error) is a dictionary, the
              :func:`repoze.bfg.traversal.model_path` function will
              attempt to append it to a string and it will cause a
              :exc:`TypeError`.

    .. note:: The :term:`root` model *must* have a ``__name__``
              attribute with a value of either ``None`` or the empty
              string for paths to be generated properly.  If the root
              model has a non-null ``__name__`` attribute, its name
              will be prepended to the generated path rather than a
              single leading '/' character.
    """
    # joining strings is a bit expensive so we delegate to a function
    # which caches the joined result for us
    return _join_path_tuple(model_path_tuple(model, *elements))

def traverse(model, path):
    """Given a model object as ``model`` and a string or tuple
    representing a path as ``path`` (such as the return value of
    :func:`repoze.bfg.traversal.model_path` or
    :func:`repoze.bfg.traversal.model_path_tuple` or the value of
    ``request.environ['PATH_INFO']``), return a dictionary with the
    keys ``context``, ``root``, ``view_name``, ``subpath``,
    ``traversed``, ``virtual_root``, and ``virtual_root_path``.

    A definition of each value in the returned dictionary:

    - ``context``: The :term:`context` (a :term:`model` object) found
      via traversal or url dispatch.  If the ``path`` passed in is the
      empty string, the value of the ``model`` argument passed to this
      function is returned.

    - ``root``: The model object at which :term:`traversal` begins.
      If the ``model`` passed in was found via url dispatch or if the
      ``path`` passed in was relative (non-absolute), the value of the
      ``model`` argument passed to this function is returned.

    - ``view_name``: The :term:`view name` found during
      :term:`traversal` or :term:`url dispatch`; if the ``model`` was
      found via traversal, this is usually a representation of the
      path segment which directly follows the path to the ``context``
      in the ``path``.  The ``view_name`` will be a Unicode object or
      the empty string.  The ``view_name`` will be the empty string if
      there is no element which follows the ``context`` path.  An
      example: if the path passed is ``/foo/bar``, and a context
      object is found at ``/foo`` (but not at ``/foo/bar``), the 'view
      name' will be ``u'bar'``.  If the ``model`` was found via
      urldispatch, the view_name will be the name the route found was
      registered with.

    - ``subpath``: For a ``model`` found via :term:`traversal`, this
      is a sequence of path segments found in the ``path`` that follow
      the ``view_name`` (if any).  Each of these items is a Unicode
      object.  If no path segments follow the ``view_name``, the
      subpath will be the empty sequence.  An example: if the path
      passed is ``/foo/bar/baz/buz``, and a context object is found at
      ``/foo`` (but not ``/foo/bar``), the 'view name' will be
      ``u'bar'`` and the :term:`subpath` will be ``[u'baz', u'buz']``.
      For a ``model`` found via url dispatch, the subpath will be a
      sequence of values discerned from ``*subpath`` in the route
      pattern matched or the empty sequence.

    - ``traversed``: The sequence of path elements traversed from the
      root to find the ``context`` object during :term:`traversal`.
      Each of these items is a Unicode object.  If no path segments
      were traversed to find the ``context`` object (e.g. if the
      ``path`` provided is the empty string), the ``traversed`` value
      will be the empty sequence.  If the ``model`` is a model found
      via :term:`url dispatch`, traversed will be None.

    - ``virtual_root``: A model object representing the 'virtual' root
      of the object graph being traversed during :term:`traversal`.
      See :ref:`vhosting_chapter` for a definition of the virtual root
      object.  If no virtual hosting is in effect, and the ``path``
      passed in was absolute, the ``virtual_root`` will be the
      *physical* root object (the object at which :term:`traversal`
      begins).  If the ``model`` passed in was found via :term:`URL
      dispatch` or if the ``path`` passed in was relative, the
      ``virtual_root`` will always equal the ``root`` object (the
      model passed in).

    - ``virtual_root_path`` -- If :term:`traversal` was used to find
      the ``model``, this will be the sequence of path elements
      traversed to find the ``virtual_root`` object.  Each of these
      items is a Unicode object.  If no path segments were traversed
      to find the ``virtual_root`` object (e.g. if virtual hosting is
      not in effect), the ``traversed`` value will be the empty list.
      If url dispatch was used to find the ``model``, this will be
      ``None``.

    If the path cannot be resolved, a :exc:`KeyError` will be raised.

    Rules for passing a *string* as the ``path`` argument: if the
    first character in the path string is the with the ``/``
    character, the path will considered absolute and the graph
    traversal will start at the root object.  If the first character
    of the path string is *not* the ``/`` character, the path is
    considered relative and graph traversal will begin at the model
    object supplied to the function as the ``model`` argument.  If an
    empty string is passed as ``path``, the ``model`` passed in will
    be returned.  Model path strings must be escaped in the following
    manner: each Unicode path segment must be encoded as UTF-8 and as
    each path segment must escaped via Python's :mod:`urllib.quote`.
    For example, ``/path/to%20the/La%20Pe%C3%B1a`` (absolute) or
    ``to%20the/La%20Pe%C3%B1a`` (relative).  The
    :func:`repoze.bfg.traversal.model_path` function generates strings
    which follow these rules (albeit only absolute ones).

    Rules for passing a *tuple* as the ``path`` argument: if the first
    element in the path tuple is the empty string (for example ``('',
    'a', 'b', 'c')``, the path is considered absolute and the graph
    traversal will start at the graph root object.  If the first
    element in the path tuple is not the empty string (for example
    ``('a', 'b', 'c')``), the path is considered relative and graph
    traversal will begin at the model object supplied to the function
    as the ``model`` argument.  If an empty sequence is passed as
    ``path``, the ``model`` passed in itself will be returned.  No
    URL-quoting or UTF-8-encoding of individual path segments within
    the tuple is required (each segment may be any string or unicode
    object representing a model name).

    Explanation of the conversion of ``path`` segment values to
    Unicode during traversal: Each segment is URL-unquoted, and
    decoded into Unicode. Each segment is assumed to be encoded using
    the UTF-8 encoding (or a subset, such as ASCII); a
    :exc:`TypeError` is raised if a segment cannot be decoded.  If a
    segment name is empty or if it is ``.``, it is ignored.  If a
    segment name is ``..``, the previous segment is deleted, and the
    ``..`` is ignored.  As a result of this process, the return values
    ``view_name``, each element in the ``subpath``, each element in
    ``traversed``, and each element in the ``virtual_root_path`` will
    be Unicode as opposed to a string, and will be URL-decoded.
    """

    if hasattr(path, '__iter__'):
        # the traverser factory expects PATH_INFO to be a string, not
        # unicode and it expects path segments to be utf-8 and
        # urlencoded (it's the same traverser which accepts PATH_INFO
        # from user agents; user agents always send strings).
        if path:
            path = _join_path_tuple(tuple(path))
        else:
            path = ''

    if path and path[0] == '/':
        model = find_root(model)

    request = Request.blank(path)
    reg = get_current_registry()
    request.registry = reg
    traverser = reg.queryAdapter(model, ITraverser)
    if traverser is None:
        traverser = ModelGraphTraverser(model)

    return traverser(request)

def model_path_tuple(model, *elements):
    """
    Return a tuple representing the absolute physical path of the
    ``model`` object based on its position in an object graph, e.g
    ``('', 'foo', 'bar')``.  Any positional arguments passed in as
    ``elements`` will be appended as elements in the tuple
    representing the model path.  For instance, if the model's
    path is ``('', 'foo', 'bar')`` and elements equals ``('a', 'b')``,
    the returned tuple will be ``('', 'foo', 'bar', 'a', b')``.  The
    first element of this tuple will always be the empty string (a
    leading empty string element in a path tuple represents that the
    path is absolute).

    This function is a logical inverse of
    :func:`repoze.bfg.traversal.find_model`: it can be used to
    generate path references that can later be resolved that function.

    The ``model`` passed in *must* be :term:`location`-aware.

    .. note:: Each segment in the path tuple returned will equal the
              ``__name__`` attribute of the model it represents within
              the graph.  Each of these segments *should* be a unicode
              or string object (as per the contract of
              :term:`location`-awareness).  However, no conversion or
              safety checking of model names is performed.  For
              instance, if one of the models in your graph has a
              ``__name__`` which (by error) is a dictionary, that
              dictionary will be placed in the path tuple; no warning
              or error will be given.

    .. note:: The :term:`root` model *must* have a ``__name__``
              attribute with a value of either ``None`` or the empty
              string for path tuples to be generated properly.  If
              the root model has a non-null ``__name__`` attribute,
              its name will be the first element in the generated
              path tuple rather than the empty string.
    """
    return tuple(_model_path_list(model, *elements))

def _model_path_list(model, *elements):
    """ Implementation detail shared by model_path and model_path_tuple """
    path = [loc.__name__ or '' for loc in lineage(model)]
    path.reverse()
    path.extend(elements)
    return path

def virtual_root(model, request):
    """
    Provided any :term:`model` and a :term:`request` object, return
    the model object representing the :term:`virtual root` of the
    current :term:`request`.  Using a virtual root in a
    :term:`traversal` -based :mod:`repoze.bfg` application permits
    rooting, for example, the object at the traversal path ``/cms`` at
    ``http://example.com/`` instead of rooting it at
    ``http://example.com/cms/``.

    If the ``model`` passed in is a context obtained via
    :term:`traversal`, and if the ``HTTP_X_VHM_ROOT`` key is in the
    WSGI environment, the value of this key will be treated as a
    'virtual root path': the :func:`repoze.bfg.traversal.find_model`
    API will be used to find the virtual root object using this path;
    if the object is found, it will be returned.  If the
    ``HTTP_X_VHM_ROOT`` key is is not present in the WSGI environment,
    the physical :term:`root` of the graph will be returned instead.

    Virtual roots are not useful at all in applications that use
    :term:`URL dispatch`. Contexts obtained via URL dispatch don't
    really support being virtually rooted (each URL dispatch context
    is both its own physical and virtual root).  However if this API
    is called with a ``model`` argument which is a context obtained
    via URL dispatch, the model passed in will be returned
    unconditionally."""
    try:
        reg = request.registry
    except AttributeError:
        reg = get_current_registry() # b/c
    urlgenerator = reg.queryMultiAdapter((model, request), IContextURL)
    if urlgenerator is None:
        urlgenerator = TraversalContextURL(model, request)
    return urlgenerator.virtual_root()

@lru_cache(1000)
def traversal_path(path):
    """ Given a ``PATH_INFO`` string (slash-separated path segments),
    return a tuple representing that path which can be used to
    traverse a graph.  The ``PATH_INFO`` is split on slashes, creating
    a list of segments.  Each segment is URL-unquoted, and decoded
    into Unicode. Each segment is assumed to be encoded using the
    UTF-8 encoding (or a subset, such as ASCII); a :exc:`TypeError` is
    raised if a segment cannot be decoded.  If a segment name is empty
    or if it is ``.``, it is ignored.  If a segment name is ``..``,
    the previous segment is deleted, and the ``..`` is ignored.
    Examples:

    ``/``

        ()

    ``/foo/bar/baz``

        (u'foo', u'bar', u'baz')

    ``foo/bar/baz``

        (u'foo', u'bar', u'baz')

    ``/foo/bar/baz/``

        (u'foo', u'bar', u'baz')

    ``/foo//bar//baz/``

        (u'foo', u'bar', u'baz')

    ``/foo/bar/baz/..``

        (u'foo', u'bar')

    ``/my%20archives/hello``

        (u'my archives', u'hello')

    ``/archives/La%20Pe%C3%B1a``

        (u'archives', u'<unprintable unicode>')

    .. note:: This function does not generate the same type of tuples
              that :func:`repoze.bfg.traversal.model_path_tuple` does.
              In particular, the leading empty string is not present
              in the tuple it returns, unlike tuples returned by
              :func:`repoze.bfg.traversal.model_path_tuple`.  As a
              result, tuples generated by ``traversal_path`` are not
              resolveable by the
              :func:`repoze.bfg.traversal.find_model` API.
              ``traversal_path`` is a function mostly used by the
              internals of :mod:`repoze.bfg` and by people writing
              their own traversal machinery, as opposed to users
              writing applications in :mod:`repoze.bfg`.
    """
    path = path.rstrip('/')
    path = path.lstrip('/')
    clean = []
    for segment in path.split('/'):
        segment = urllib.unquote(segment) # deal with spaces in path segment
        if not segment or segment=='.':
            continue
        elif segment == '..':
            del clean[-1]
        else:
            try:
                segment = segment.decode('utf-8')
            except UnicodeDecodeError:
                raise TypeError('Could not decode path segment %r using the '
                                'UTF-8 decoding scheme' % segment)
            clean.append(segment)
    return tuple(clean)

_segment_cache = {}

def quote_path_segment(segment):
    """ Return a quoted representation of a 'path segment' (such as
    the string ``__name__`` attribute of a model) as a string.  If the
    ``segment`` passed in is a unicode object, it is converted to a
    UTF-8 string, then it is URL-quoted using Python's
    ``urllib.quote``.  If the ``segment`` passed in is a string, it is
    URL-quoted using Python's :mod:`urllib.quote`.  If the segment
    passed in is not a string or unicode object, an error will be
    raised.  The return value of ``quote_path_segment`` is always a
    string, never Unicode.

    .. note:: The return value for each segment passed to this
              function is cached in a module-scope dictionary for
              speed: the cached version is returned when possible
              rather than recomputing the quoted version.  No cache
              emptying is ever done for the lifetime of an
              application, however.  If you pass arbitrary
              user-supplied strings to this function (as opposed to
              some bounded set of values from a 'working set' known to
              your application), it may become a memory leak.
    """
    # The bit of this code that deals with ``_segment_cache`` is an
    # optimization: we cache all the computation of URL path segments
    # in this module-scope dictionary with the original string (or
    # unicode value) as the key, so we can look it up later without
    # needing to reencode or re-url-quote it
    try:
        return _segment_cache[segment]
    except KeyError:
        if segment.__class__ is unicode: # isinstance slighly slower (~15%)
            result = url_quote(segment.encode('utf-8'))
        else:
            result = url_quote(segment)
        # we don't need a lock to mutate _segment_cache, as the below
        # will generate exactly one Python bytecode (STORE_SUBSCR)
        _segment_cache[segment] = result
        return result

class ModelGraphTraverser(object):
    """ A model graph traverser that should be used (for speed) when
    every object in the graph supplies a ``__name__`` and
    ``__parent__`` attribute (ie. every object in the graph is
    :term:`location` aware) ."""

    implements(ITraverser)

    def __init__(self, root):
        self.root = root

    def __call__(self, request):
        try:
            environ = request.environ
        except AttributeError:
            # In BFG 1.0 and before, this API expected an environ
            # rather than a request; some bit of code may still be
            # passing us an environ.  If so, deal.
            environ = request

        if 'bfg.routes.matchdict' in environ:
            matchdict = environ['bfg.routes.matchdict']

            path = matchdict.get('traverse', '/')
            if hasattr(path, '__iter__'):
                # this is a *traverse stararg (not a :traverse)
                path = '/'.join([quote_path_segment(x) for x in path]) or '/'

            subpath = matchdict.get('subpath', ())
            if not hasattr(subpath, '__iter__'):
                # this is not a *subpath stararg (just a :subpath)
                subpath = traversal_path(subpath)

        else:
            # this request did not match a Routes route
            subpath = ()
            try:
                path = environ['PATH_INFO'] or '/'
            except KeyError:
                path = '/'

        if VH_ROOT_KEY in environ:
            vroot_path = environ[VH_ROOT_KEY]
            vroot_tuple = traversal_path(vroot_path)
            vpath = vroot_path + path
            vroot_idx = len(vroot_tuple) -1
        else:
            vroot_tuple = ()
            vpath = path
            vroot_idx = -1

        root = self.root
        ob = vroot = root

        if vpath == '/' or (not vpath):
            # prevent a call to traversal_path if we know it's going
            # to return the empty tuple
            vpath_tuple = ()
        else:
            # we do dead reckoning here via tuple slicing instead of
            # pushing and popping temporary lists for speed purposes
            # and this hurts readability; apologies
            i = 0
            vpath_tuple = traversal_path(vpath)
            for segment in vpath_tuple:
                if segment[:2] =='@@':
                    return {'context':ob,
                            'view_name':segment[2:],
                            'subpath':vpath_tuple[i+1:],
                            'traversed':vpath_tuple[:vroot_idx+i+1],
                            'virtual_root':vroot,
                            'virtual_root_path':vroot_tuple,
                            'root':root}
                try:
                    getitem = ob.__getitem__
                except AttributeError:
                    return {'context':ob,
                            'view_name':segment,
                            'subpath':vpath_tuple[i+1:],
                            'traversed':vpath_tuple[:vroot_idx+i+1],
                            'virtual_root':vroot,
                            'virtual_root_path':vroot_tuple,
                            'root':root}

                try:
                    next = getitem(segment)
                except KeyError:
                    return {'context':ob,
                            'view_name':segment,
                            'subpath':vpath_tuple[i+1:],
                            'traversed':vpath_tuple[:vroot_idx+i+1],
                            'virtual_root':vroot,
                            'virtual_root_path':vroot_tuple,
                            'root':root}
                if i == vroot_idx: 
                    vroot = next
                ob = next
                i += 1

        return {'context':ob, 'view_name':u'', 'subpath':subpath,
                'traversed':vpath_tuple, 'virtual_root':vroot,
                'virtual_root_path':vroot_tuple, 'root':root}

class TraversalContextURL(object):
    """ The IContextURL adapter used to generate URLs for a context
    object obtained via graph traversal"""
    implements(IContextURL)

    vroot_varname = VH_ROOT_KEY

    def __init__(self, context, request):
        self.context = context
        self.request = request

    def virtual_root(self):
        environ = self.request.environ
        vroot_varname = self.vroot_varname
        if vroot_varname in environ:
            return find_model(self.context, environ[vroot_varname])
        # shortcut instead of using find_root; we probably already
        # have it on the request
        try:
            return self.request.root
        except AttributeError:
            return find_root(self.context)
        
    def __call__(self):
        """ Generate a URL based on the :term:`lineage` of a
        :term:`model` object obtained via :term:`traversal`.  If any
        model in the context lineage has a Unicode name, it will be
        converted to a UTF-8 string before being attached to the URL.
        If a ``HTTP_X_VHM_ROOT`` key is present in the WSGI
        environment, its value will be treated as a 'virtual root
        path': the path of the URL generated by this will be
        left-stripped of this virtual root path value.
        """
        path = model_path(self.context)
        if path != '/':
            path = path + '/'
        request = self.request
        environ = request.environ
        vroot_varname = self.vroot_varname

        # if the path starts with the virtual root path, trim it out
        if vroot_varname in environ:
            vroot_path = environ[vroot_varname]
            if path.startswith(vroot_path):
                path = path[len(vroot_path):]

        app_url = request.application_url # never ends in a slash
        return app_url + path

@lru_cache(1000)
def _join_path_tuple(tuple):
    return tuple and '/'.join([quote_path_segment(x) for x in tuple]) or '/'

class DefaultRootFactory:
    __parent__ = None
    __name__ = None
    def __init__(self, request):
        matchdict = getattr(request, 'matchdict', {})
        # provide backwards compatibility for applications which
        # used routes (at least apps without any custom "context
        # factory") in BFG 0.9.X and before
        self.__dict__.update(matchdict)
