"""Bounded existing API/source contracts; no credentials or activation here."""
import urllib.parse
import urllib.request
from operations_ledger_v1 import GlobalStop
from independent_runner import NoRedirect
from text_source_capture import capture_selected
from research_quality import capture_children

PARAMS=dict(tournaments='fall-futureeval-2026',limit='25',statuses='open',
    forecast_type='binary,multiple_choice,numeric,discrete',include_descriptions='true')


def list_posts(client,tournament_id,*,guard,max_pages=3):
    if tournament_id!=33121 or type(tournament_id) is not int:
        raise GlobalStop('Explicit supported tournament required')
    if type(max_pages) is not int or not 1<=max_pages<=3:raise GlobalStop('Bounded page count required')
    offset=0;posts=[];seen=set()
    for i in range(max_pages):
        params=dict(PARAMS)
        if offset:params['offset']=str(offset)
        guard();page=client.http.call('posts/?'+urllib.parse.urlencode(params));guard()
        if not isinstance(page,dict) or not isinstance(page.get('results'),list) or len(page['results'])>25:
            raise GlobalStop('Bounded posts page required')
        for post in page['results']:
            if not isinstance(post,dict) or type(post.get('id')) is not int or post['id']<=0:
                raise GlobalStop('Post identity missing')
            if post['id'] in seen:raise GlobalStop('Repeated post across listing pages')
            seen.add(post['id']);posts.append(post)
        # Countless pagination can advertise another page after the last one.
        if len(page['results'])<25 or not page.get('next'):return posts
        parsed=urllib.parse.urlsplit(page['next']);items=urllib.parse.parse_qsl(parsed.query,keep_blank_values=True)
        expected=dict(PARAMS,offset=str(offset+25))
        if (parsed.scheme!='https' or parsed.netloc!='www.metaculus.com' or parsed.path!='/api/posts/'
                or parsed.fragment or len(items)!=len(expected) or dict(items)!=expected):
            raise GlobalStop('Listing next-page contract changed')
        if i==max_pages-1:raise GlobalStop('Listing page bound reached; no partial queue')
        offset+=25
    raise GlobalStop('Listing incomplete')


class SourceResponse:
    def __init__(self,response,guard):self.response,self.guard=response,guard
    def __enter__(self):self.response.__enter__();return self
    def __exit__(self,*args):return self.response.__exit__(*args)
    def __getattr__(self,name):return getattr(self.response,name)
    def read(self,*args):
        self.guard();data=self.response.read(*args);self.guard();return data


def source_factory(*,opener=None,resolver=None):
    """Use existing redirect/public-address/media bounds for every source."""
    opener=opener or urllib.request.build_opener(NoRedirect())
    def factory(host):
        options={} if resolver is None else dict(resolver=resolver)
        def open_request(req,**kwargs):
            if req.get_method()!='GET' or any(k.lower() in ('authorization','cookie','x-api-key') for k in req.headers):
                raise GlobalStop('Public source GET without credentials required')
            host.before_effect()
            response=opener.open(req,**kwargs)
            try:host.guard_local()
            except BaseException:response.close();raise
            return SourceResponse(response,host.guard_local)
        return (
            lambda q,urls,searched,folder:capture_selected(q,urls,searched,folder,open_request=open_request,**options),
            lambda offered,selected,docs,folder:capture_children(offered,selected,docs,folder,open_request=open_request,**options),
        )
    return factory
